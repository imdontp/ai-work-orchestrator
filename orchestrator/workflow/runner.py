"""Executes a workflow graph.

The runner is the deterministic half of the system. It decides what runs, in what
order, under what permissions, with what timeout, and whether a result is believed.
Workers supply judgment and implementation; they never decide any of the above.

Shape of a run::

    advance()  ->  runs nodes until a human is needed or the run ends
    decide()   ->  records an approval decision
    advance()  ->  continues

It pauses rather than blocks, so a run outlives the process that started it: everything
needed to continue is in the :class:`~orchestrator.workflow.store.RunRecord`.

Four rules are enforced here rather than trusted to a worker:

- A worker reporting ``completed`` moves to verification, never straight to completed.
- Verification is re-run mechanically. ``verification.claimed_passed`` is a claim.
- A write node runs in its own worktree under :class:`WorkspaceContainment`; a write
  landing outside it fails the run whatever the worker reported.
- Repair rounds are bounded by the workflow's ``max_repair_rounds``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from execution.process_manager import ProcessManager
from execution.verifier import VerificationCommand, VerificationRunner
from execution.workspace_guard import WorkspaceContainment
from execution.worktree_manager import Worktree, WorktreeManager
from orchestrator.context_builder.builder import ContextPackageBuilder
from orchestrator.domain.models import ApprovalRisk, Task, TaskState
from orchestrator.state_machine import TaskStateMachine
from orchestrator.workflow.definition import (
    ApprovalKind,
    SessionPolicy,
    WorkflowDefinition,
    WorkflowNode,
)
from orchestrator.workflow.store import ApprovalRequest, RunEvent, RunRecord, RunStore
from workers.base import WorkerAdapter, WorkerRequest, WorkerResult


class WorkflowRunError(RuntimeError):
    """The run cannot proceed and the caller must intervene."""


@dataclass(frozen=True)
class NodeOutcome:
    """What a node produced, before the runner decides whether to believe it."""

    artifact: Any
    session_id: str | None = None
    failed: bool = False
    failure_reason: str | None = None
    #: Set when the node's own output asks for changes, e.g. a review verdict.
    requests_changes: bool = False


@dataclass
class RunnerConfig:
    """Everything the runner needs that is not the workflow or the task."""

    repository: Path
    workspace_root: Path
    run_root: Path
    #: Commands re-run mechanically after a write node. Empty means nothing is proven.
    verification_commands: list[VerificationCommand] = field(default_factory=list)
    node_timeout_seconds: int = 1800
    #: Model per worker requirement, e.g. {"claude_code": "sonnet"}. Keyed rather than
    #: a single value because a model name is meaningful only to one provider: a live
    #: smoke test failed with `kind=model` when a Claude alias reached Codex. ADR-004
    #: keeps Model and Worker Adapter separate; one shared field conflated them.
    #: A worker with no entry gets no --model flag and uses its own default.
    models: dict[str, str] = field(default_factory=dict)
    #: Tools granted to a write node. Read-only nodes always get none.
    write_tools: tuple[str, ...] = ("Read", "Write", "Edit", "Bash")


class WorkflowRunner:
    def __init__(
        self,
        *,
        workflow: WorkflowDefinition,
        task: Task,
        config: RunnerConfig,
        adapters: dict[str, WorkerAdapter],
        store: RunStore | None = None,
        worktree_manager: WorktreeManager | None = None,
        verification_runner: VerificationRunner | None = None,
        context_builder: ContextPackageBuilder | None = None,
    ) -> None:
        self.workflow = workflow
        self.task = task
        self.config = config
        self.adapters = adapters
        self.store = store or RunStore(config.run_root)
        self.worktrees = worktree_manager or WorktreeManager(
            config.repository, config.workspace_root
        )
        self.verifier = verification_runner or VerificationRunner(ProcessManager())
        self.context_builder = context_builder or ContextPackageBuilder()

    # -- lifecycle ---------------------------------------------------------------

    def create_run(self, run_id: str | None = None) -> RunRecord:
        record = RunRecord(
            run_id=run_id or f"RUN-{uuid4().hex[:12]}",
            task_id=self.task.task_id,
            workflow_id=self.workflow.workflow_id,
            workflow_version=self.workflow.version,
        )
        self.store.create(record)
        self._event(record, "run_created", detail={"workflow": self.workflow.workflow_id})
        return record

    async def advance(self, run_id: str) -> RunRecord:
        """Run nodes until a human is needed, the run ends, or something fails."""
        record = self.store.load(run_id)

        if record.is_terminal:
            return record
        if record.pending_approval is not None:
            return record

        if record.task_state is TaskState.PENDING:
            self._transition(record, TaskState.READY)

        while True:
            node = self._next_node(record)
            if node is None:
                return self._finish(record)

            if node.is_human:
                self._request_approval(record, node, ApprovalKind.FINAL_RESULT)
                return record

            try:
                outcome = await self._execute_node(record, node)
            except Exception as exc:  # noqa: BLE001 - recorded, then surfaced as state
                self._fail(record, node, f"{type(exc).__name__}: {exc}")
                return record

            if outcome.failed:
                if self._can_repair(record):
                    self._start_repair(record, node, outcome.failure_reason or "node failed")
                    continue
                self._fail(record, node, outcome.failure_reason or "node failed")
                return record

            self._record_artifact(record, node, outcome)

            if outcome.requests_changes:
                if self._can_repair(record):
                    self._start_repair(record, node, "review requested changes")
                    continue
                self._fail(record, node, "review requested changes and the repair budget is spent")
                return record

            if node.approval_after is not None:
                self._request_approval(record, node, node.approval_after)
                return record

        # unreachable

    def decide(self, run_id: str, decision: str, *, reason: str = "") -> RunRecord:
        """Record a human decision on the pending approval.

        ``decision`` is one of ``approve``, ``request_changes`` or ``reject`` — the
        values contracts/approval-package.schema.json allows.
        """
        record = self.store.load(run_id)
        approval = record.pending_approval
        if approval is None:
            raise WorkflowRunError(f"run {run_id} is not waiting for a decision")
        if decision not in {"approve", "request_changes", "reject"}:
            raise WorkflowRunError(f"unknown decision: {decision}")

        self._event(
            record,
            "approval_decided",
            node_id=approval.resume_after_node,
            detail={"approval_id": approval.approval_id, "decision": decision, "reason": reason},
        )
        record.pending_approval = None

        if decision == "reject":
            self._transition(record, TaskState.CANCELLED)
            record.failure = reason or "rejected by human"
        elif decision == "request_changes":
            node = self.workflow.node(approval.resume_after_node)
            if self._can_repair(record):
                self._start_repair(record, node, reason or "human requested changes")
            else:
                self._fail(record, node, "human requested changes and the repair budget is spent")
        elif approval.approval_type == ApprovalKind.FINAL_RESULT.value:
            self._transition(record, TaskState.COMPLETED)
        else:
            self._transition(record, TaskState.READY)

        self.store.save(record)
        return record

    # -- node execution ----------------------------------------------------------

    async def _execute_node(self, record: RunRecord, node: WorkflowNode) -> NodeOutcome:
        self._event(record, "node_started", node_id=node.id)

        if node.worker_requirement == "local_tool_runner":
            self._enter(record, TaskState.VERIFYING)
            return await self._run_verification(record, node)

        self._enter(record, TaskState.RUNNING)
        return await self._run_worker(record, node)

    async def _run_worker(self, record: RunRecord, node: WorkflowNode) -> NodeOutcome:
        adapter = self.adapters.get(node.worker_requirement)
        if adapter is None:
            raise WorkflowRunError(
                f"node {node.id} requires worker {node.worker_requirement!r}, "
                f"which is not configured. Available: {sorted(self.adapters)}"
            )

        worktree = self._worktree_for(record, node)
        workspace = worktree.path if worktree is not None else self.config.repository
        containment = self._containment_for(worktree)

        request = self._build_request(record, node, workspace)

        if containment is not None:
            containment.arm()
            self._event(
                record,
                "containment_armed",
                node_id=node.id,
                detail={"barrier": containment.barrier.mechanism, "workspace": str(workspace)},
            )
        try:
            handle = await adapter.start(request)
            self._event(
                record, "worker_started", node_id=node.id, detail={"pid": handle.process_id}
            )
            result = await adapter.collect(handle)
        finally:
            report = containment.disarm() if containment is not None else None

        if report is not None and not report.contained:
            # A worker that wrote outside its workspace is not trusted about anything
            # else it reported, so this outranks its own success claim.
            violations = [str(v) for v in report.violations]
            self._event(
                record,
                "containment_violation",
                node_id=node.id,
                detail={"violations": violations},
            )
            return NodeOutcome(
                artifact=None,
                failed=True,
                failure_reason=f"writes outside the workspace: {violations}",
            )

        if result.session_id:
            record.sessions[node.worker_requirement] = result.session_id

        if result.reported_error or result.exit_code != 0:
            return NodeOutcome(
                artifact=self._worker_artifact(node, result),
                session_id=result.session_id,
                failed=True,
                failure_reason=(
                    f"{node.worker_requirement} failed: exit={result.exit_code} "
                    f"kind={result.error_kind}"
                ),
            )

        artifact = self._worker_artifact(node, result)
        return NodeOutcome(
            artifact=artifact,
            session_id=result.session_id,
            requests_changes=_asks_for_changes(artifact),
        )

    async def _run_verification(self, record: RunRecord, node: WorkflowNode) -> NodeOutcome:
        """Re-run the configured commands. The worker's claim is not evidence."""
        worktree = record.worktree
        workspace = Path(worktree["path"]) if worktree else self.config.repository

        claimed = _claimed_passed(self._dependency_artifacts(record, node))

        if not self.config.verification_commands:
            # Say so loudly. A run with nothing to verify has proven nothing, and that
            # must not read the same as a run that passed its checks.
            self._event(record, "verification_skipped", node_id=node.id)
            return NodeOutcome(
                artifact={
                    "schema_version": "1.0",
                    "verified": False,
                    "reason": "no verification commands configured",
                    "claimed_passed": claimed,
                    "commands": [],
                },
                failed=True,
                failure_reason="no verification commands configured",
            )

        result = await self.verifier.run(
            commands=self.config.verification_commands,
            workspace=workspace,
            output_dir=self.store.log_dir(record.run_id),
        )
        commands = [
            {
                "args": command.args,
                "exit_code": process.exit_code,
                "timed_out": process.timed_out,
            }
            for command, process in zip(
                self.config.verification_commands, result.command_results, strict=False
            )
        ]
        self._event(
            record,
            "verification_finished",
            node_id=node.id,
            detail={"passed": result.passed, "claimed_passed": claimed},
        )

        artifact = {
            "schema_version": "1.0",
            "verified": result.passed,
            "claimed_passed": claimed,
            "commands": commands,
        }
        if not result.passed:
            return NodeOutcome(
                artifact=artifact,
                failed=True,
                failure_reason="verification commands failed",
            )
        return NodeOutcome(artifact=artifact)

    # -- request construction ----------------------------------------------------

    def _build_request(
        self, record: RunRecord, node: WorkflowNode, workspace: Path
    ) -> WorkerRequest:
        package = self.context_builder.build(
            task=self.task,
            node=node,
            workflow=self.workflow,
            artifacts=self._dependency_artifacts(record, node),
            artifact_paths={
                node_id: str(self.store.artifact_path(record.run_id, filename))
                for node_id, filename in record.artifacts.items()
            },
        )

        resume_from: str | None = None
        if node.session_policy is SessionPolicy.REUSE:
            # Claude Code sessions are bound to the directory they were created in, so
            # only resume when the node runs in the same workspace as before.
            resume_from = record.sessions.get(node.worker_requirement)
        elif node.session_policy is SessionPolicy.NEW:
            # Independent review must not inherit the implementer's context.
            resume_from = None

        return WorkerRequest(
            task_id=self.task.task_id,
            run_id=f"{record.run_id}-{node.id}",
            prompt=_render_prompt(node, package),
            workspace=workspace,
            log_dir=self.store.log_dir(record.run_id),
            output_schema_path=None,
            timeout_seconds=self.config.node_timeout_seconds,
            environment={},
            model=self.config.models.get(node.worker_requirement),
            resume_from=resume_from,
            allowed_tools=self.config.write_tools if node.needs_worktree else (),
            filesystem_access="scoped_write" if node.needs_worktree else "read_only",
        )

    def _worktree_for(self, record: RunRecord, node: WorkflowNode) -> Worktree | None:
        if not node.needs_worktree:
            return None
        if record.worktree is not None:
            # A repair round reuses the worktree so the branch history and any resumed
            # session stay coherent.
            return Worktree(
                run_id=record.worktree["run_id"],
                repository=Path(record.worktree["repository"]),
                run_dir=Path(record.worktree["run_dir"]),
                path=Path(record.worktree["path"]),
                branch=record.worktree["branch"],
                base_ref=record.worktree["base_ref"],
            )

        worktree = self.worktrees.create(record.run_id)
        self.worktrees.lock(worktree, f"{record.run_id} in flight")
        record.worktree = {
            "run_id": worktree.run_id,
            "repository": str(worktree.repository),
            "run_dir": str(worktree.run_dir),
            "path": str(worktree.path),
            "branch": worktree.branch,
            "base_ref": worktree.base_ref,
        }
        self._event(
            record,
            "worktree_created",
            node_id=node.id,
            detail={"path": str(worktree.path), "branch": worktree.branch},
        )
        return worktree

    def _containment_for(self, worktree: Worktree | None) -> WorkspaceContainment | None:
        if worktree is None:
            return None
        return WorkspaceContainment(
            worktree.path,
            protected_roots=[self.config.repository],
            allowed_paths=worktree.git_allowances,
        )

    # -- bookkeeping -------------------------------------------------------------

    def _next_node(self, record: RunRecord) -> WorkflowNode | None:
        done = set(record.completed_nodes)
        for node_id in self.workflow.execution_order():
            if node_id in done:
                continue
            node = self.workflow.node(node_id)
            if all(dependency in done for dependency in node.depends_on):
                return node
        return None

    def _dependency_artifacts(self, record: RunRecord, node: WorkflowNode) -> dict[str, Any]:
        artifacts: dict[str, Any] = {}
        for dependency_id in node.depends_on:
            filename = record.artifacts.get(dependency_id)
            if filename:
                artifacts[dependency_id] = self.store.read_artifact(record.run_id, filename)
        return artifacts

    def _record_artifact(
        self, record: RunRecord, node: WorkflowNode, outcome: NodeOutcome
    ) -> None:
        filename = f"{node.id}-{node.expected_artifact}"
        self.store.write_artifact(record.run_id, filename, outcome.artifact)
        record.artifacts[node.id] = filename
        record.completed_nodes.append(node.id)
        self._event(record, "node_completed", node_id=node.id, detail={"artifact": filename})
        self.store.save(record)

    def _worker_artifact(self, node: WorkflowNode, result: WorkerResult) -> Any:
        """Prefer the worker's structured outcome; fall back to a described envelope."""
        if result.result_path is not None and result.result_path.is_file():
            payload = json.loads(result.result_path.read_text(encoding="utf-8"))
            structured = payload.get("structured_result")
            if structured is not None:
                return structured
            text = payload.get("result_text")
            if isinstance(text, str):
                parsed = _maybe_json(text)
                if parsed is not None:
                    return parsed
            return payload
        return {
            "node": node.id,
            "exit_code": result.exit_code,
            "error_kind": result.error_kind,
        }

    def _request_approval(
        self, record: RunRecord, node: WorkflowNode, kind: ApprovalKind
    ) -> None:
        evidence = [
            str(self.store.artifact_path(record.run_id, filename))
            for filename in record.artifacts.values()
        ]
        approval = ApprovalRequest(
            approval_id=f"APPROVAL-{uuid4().hex[:8]}",
            run_id=record.run_id,
            approval_type=kind.value,
            risk_level=self._risk_for(kind).value,
            summary=f"{self.workflow.workflow_id}: {kind.value} approval after {node.id}",
            changes=[record.worktree["branch"]] if record.worktree else [],
            evidence=evidence,
            risks=[],
            requested_decision="approve",
            resume_after_node=node.id,
        )
        record.pending_approval = approval
        self._transition(record, TaskState.WAITING_APPROVAL)

        payload = approval.model_dump(exclude={"resume_after_node"})
        self.store.write_artifact(record.run_id, f"{approval.approval_id}.json", payload)
        self._event(
            record,
            "approval_requested",
            node_id=node.id,
            detail={"approval_id": approval.approval_id, "type": kind.value},
        )
        self.store.save(record)

    @staticmethod
    def _risk_for(kind: ApprovalKind) -> ApprovalRisk:
        if kind in {ApprovalKind.FINAL_RESULT, ApprovalKind.EXTERNAL_SIDE_EFFECT}:
            return ApprovalRisk.HIGH
        return ApprovalRisk.MEDIUM

    def _can_repair(self, record: RunRecord) -> bool:
        return record.repair_rounds < self.workflow.max_repair_rounds

    def _start_repair(self, record: RunRecord, node: WorkflowNode, reason: str) -> None:
        """Roll back to the first write node and try again, within budget."""
        record.repair_rounds += 1
        target = self._repair_entry_node()

        order = self.workflow.execution_order()
        from_index = order.index(target)
        rolled_back = [n for n in order[from_index:] if n in record.completed_nodes]
        record.completed_nodes = [n for n in record.completed_nodes if n not in rolled_back]
        for node_id in rolled_back:
            record.artifacts.pop(node_id, None)

        # A human asking for changes at a gate is not a failure, and WAITING_APPROVAL
        # has no edge to FAILED_RETRYABLE. Only take that route when the run really did
        # fail - a bad worker result, or verification that did not pass.
        if TaskStateMachine.can_transition(record.task_state, TaskState.FAILED_RETRYABLE):
            self._transition(record, TaskState.FAILED_RETRYABLE)
        self._enter(record, TaskState.READY)
        self._event(
            record,
            "repair_started",
            node_id=node.id,
            detail={
                "round": record.repair_rounds,
                "of": self.workflow.max_repair_rounds,
                "reason": reason,
                "replaying": rolled_back,
            },
        )
        self.store.save(record)

    def _repair_entry_node(self) -> str:
        for node_id in self.workflow.execution_order():
            if self.workflow.node(node_id).needs_worktree:
                return node_id
        return self.workflow.execution_order()[0]

    def _finish(self, record: RunRecord) -> RunRecord:
        # Reaching the end of the graph without an approval would mean completing a run
        # no human ever saw. The graph validator forbids that shape, so this is a guard
        # against a graph slipping through rather than an expected path.
        if record.task_state is not TaskState.COMPLETED:
            self._fail_without_node(record, "workflow ended without a final approval")
        self.store.save(record)
        return record

    def _fail(self, record: RunRecord, node: WorkflowNode, reason: str) -> None:
        self._event(record, "node_failed", node_id=node.id, detail={"reason": reason})
        self._fail_without_node(record, reason)

    def _fail_without_node(self, record: RunRecord, reason: str) -> None:
        record.failure = reason
        if TaskStateMachine.can_transition(record.task_state, TaskState.FAILED_PERMANENT):
            self._transition(record, TaskState.FAILED_PERMANENT)
        self.store.save(record)

    def _enter(self, record: RunRecord, target: TaskState) -> None:
        """Move the run into a phase, taking the one legal detour when needed.

        The task states describe the run's phase, not each node, so consecutive nodes
        of the same kind need no transition at all. Where the table has no direct edge
        - VERIFYING to RUNNING, when review follows verification - READY is the neutral
        state a run passes back through.
        """
        if record.task_state is target:
            return
        if TaskStateMachine.can_transition(record.task_state, target):
            self._transition(record, target)
            return
        if TaskStateMachine.can_transition(
            record.task_state, TaskState.READY
        ) and TaskStateMachine.can_transition(TaskState.READY, target):
            self._transition(record, TaskState.READY)
            self._transition(record, target)
            return
        raise WorkflowRunError(
            f"no path from {record.task_state.value} to {target.value}; "
            "the workflow needs a state transition the machine does not allow"
        )

    def _transition(self, record: RunRecord, target: TaskState) -> None:
        previous = record.task_state
        record.task_state = TaskStateMachine.transition(previous, target)
        self._event(
            record, "state_changed", detail={"from": previous.value, "to": target.value}
        )

    def _event(
        self,
        record: RunRecord,
        kind: str,
        *,
        node_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_event(
            record.run_id, RunEvent(kind=kind, node_id=node_id, detail=detail or {})
        )


# ---------------------------------------------------------------------------
# Artifact interpretation
# ---------------------------------------------------------------------------


def _claimed_passed(artifacts: dict[str, Any]) -> bool | None:
    """What the worker *said* about verification. Recorded, never believed."""
    for payload in artifacts.values():
        if isinstance(payload, dict):
            verification = payload.get("verification")
            if isinstance(verification, dict) and "claimed_passed" in verification:
                return bool(verification["claimed_passed"])
    return None


def _asks_for_changes(artifact: Any) -> bool:
    if not isinstance(artifact, dict):
        return False
    verdict = artifact.get("verdict")
    if isinstance(verdict, str) and verdict.lower() in {"fail", "request_changes", "changes"}:
        return True
    return artifact.get("status") == "blocked"


def _maybe_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _render_prompt(node: WorkflowNode, package: Any) -> str:
    """Render the context package as the worker's prompt.

    The agent profile's system prompt lives in ``prompts/<profile>/system.md`` and is
    applied by the adapter layer; this is the per-run payload only.
    """
    return json.dumps(package.model_dump(), indent=2, ensure_ascii=False)
