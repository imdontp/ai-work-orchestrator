"""Workflow runner behaviour.

Driven with a fake adapter and a real git repository. The point of these tests is the
orchestrator's decisions — what runs, whether a result is believed, when a human is
required — so the worker is scripted and the containment, worktree and verification
machinery underneath is the real thing.
"""

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from execution.verifier import VerificationCommand
from orchestrator.domain.models import Task, TaskPermissions, TaskState
from orchestrator.workflow.definition import WorkflowDefinition
from orchestrator.workflow.runner import RunnerConfig, WorkflowRunError, WorkflowRunner
from orchestrator.workflow.store import FilesystemRunStore, RunStore
from workers.base import (
    WorkerAdapter,
    WorkerCapabilities,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"

GRAPH = {
    "workflow_id": "test-slice",
    "version": "0.1.0",
    "max_repair_rounds": 2,
    "nodes": [
        {
            "id": "analyze",
            "agent_profile": "task-analyst",
            "worker_requirement": "claude_code",
            "expected_artifact": "analysis.json",
            "approval_after": "plan",
        },
        {
            "id": "implement",
            "agent_profile": "code-implementer",
            "worker_requirement": "codex",
            "depends_on": ["analyze"],
            "expected_artifact": "worker-result.json",
            "workspace": "isolated_worktree",
        },
        {
            "id": "verify",
            "agent_profile": "system-verifier",
            "worker_requirement": "local_tool_runner",
            "depends_on": ["implement"],
            "expected_artifact": "verification-result.json",
        },
        {
            "id": "review",
            "agent_profile": "independent-reviewer",
            "worker_requirement": "claude_code",
            "session_policy": "new",
            "depends_on": ["implement", "verify"],
            "expected_artifact": "review-result.json",
        },
        {
            "id": "final_approval",
            "agent_profile": "human-approval",
            "worker_requirement": "human",
            "depends_on": ["review"],
            "expected_artifact": "approval-decision.json",
        },
    ],
}


class FakeAdapter(WorkerAdapter):
    """A scripted worker. Returns a queued outcome per call and records requests."""

    capabilities = WorkerCapabilities(
        structured_output=True,
        stream_events=True,
        resume_session=True,
        cancel_process=True,
        scoped_write=False,
        server_mode=False,
    )

    def __init__(self, name: str, log_dir: Path) -> None:
        self.name = name
        self.log_dir = log_dir
        self.outcomes: list[dict] = []
        self.requests: list[WorkerRequest] = []
        self._counter = 0
        #: Written into the workspace before returning, to simulate real work.
        self.writes: dict[str, str] = {}
        self.escape_write: Path | None = None

    def queue(self, **payload) -> None:
        self.outcomes.append(payload)

    async def health_check(self) -> dict:
        return {"worker": self.name, "available": True}

    async def start(self, request: WorkerRequest) -> WorkerHandle:
        self.requests.append(request)
        self._counter += 1
        for name, content in self.writes.items():
            (request.workspace / name).write_text(content, encoding="utf-8")
        if self.escape_write is not None:
            self.escape_write.write_text("ESCAPED", encoding="utf-8")
        return WorkerHandle(worker_run_id=f"{self.name}-{self._counter}", process_id=1000)

    def stream_events(self, handle: WorkerHandle) -> AsyncIterator[str]:
        async def _empty() -> AsyncIterator[str]:
            return
            yield ""  # pragma: no cover

        return _empty()

    async def cancel(self, handle: WorkerHandle) -> None:
        return None

    async def collect(self, handle: WorkerHandle) -> WorkerResult:
        payload = self.outcomes.pop(0) if self.outcomes else {"structured_result": {"ok": True}}
        outcome_path = self.log_dir / f"{handle.worker_run_id}.outcome.json"
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text(json.dumps(payload), encoding="utf-8")
        return WorkerResult(
            exit_code=payload.get("exit_code", 0),
            stdout_path=outcome_path,
            stderr_path=outcome_path,
            result_path=outcome_path,
            session_id=payload.get("session_id", f"session-{self.name}"),
            reported_error=payload.get("reported_error", False),
            error_kind=payload.get("error_kind"),
        )


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@l", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        check=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "primary"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    return repo


@pytest.fixture
def task() -> Task:
    return Task(
        task_id="TASK-001",
        project_id="PROJ-001",
        task_type="feature",
        objective="Add a multiply function",
        acceptance_criteria=["multiply(2, 3) returns 6"],
        permissions=TaskPermissions(filesystem="scoped_write"),
        expected_outputs=["calc.py"],
    )


@pytest.fixture
def harness(tmp_path: Path, repository: Path, task: Task):
    workflow = WorkflowDefinition.model_validate(GRAPH)
    run_root = tmp_path / "runs"
    store = FilesystemRunStore(run_root)
    log_dir = tmp_path / "worker-logs"

    claude = FakeAdapter("claude_code", log_dir)
    codex = FakeAdapter("codex", log_dir)

    config = RunnerConfig(
        repository=repository,
        workspace_root=tmp_path / "workspaces",
        run_root=run_root,
        verification_commands=[
            VerificationCommand(args=["git", "--version"], timeout_seconds=60)
        ],
    )
    runner = WorkflowRunner(
        workflow=workflow,
        task=task,
        config=config,
        adapters={"claude_code": claude, "codex": codex},
        store=store,
    )
    (tmp_path / "workspaces").mkdir()
    return runner, claude, codex, store


def _kinds(store: RunStore, run_id: str) -> list[str]:
    return [event.kind for event in store.read_events(run_id)]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_pauses_at_the_plan_approval(harness) -> None:
    runner, claude, _, store = harness
    claude.queue(structured_result={"plan": ["add multiply"]})

    record = runner.create_run("RUN-1")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.task_state is TaskState.WAITING_APPROVAL
    assert record.pending_approval is not None
    assert record.pending_approval.approval_type == "plan"
    assert record.completed_nodes == ["analyze"]
    # Nothing after the gate ran.
    assert "implement" not in record.artifacts


def test_full_slice_reaches_completion_after_two_approvals(harness) -> None:
    runner, claude, codex, store = harness
    claude.queue(structured_result={"plan": ["add multiply"]})
    codex.queue(
        structured_result={
            "status": "completed",
            "verification": {"claimed_passed": True, "commands": ["pytest"]},
        }
    )
    claude.queue(structured_result={"verdict": "pass", "findings": []})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.task_state is TaskState.WAITING_APPROVAL
    assert record.pending_approval.approval_type == "final_result"
    assert record.completed_nodes == ["analyze", "implement", "verify", "review"]

    record = runner.decide("RUN-1", "approve")
    assert record.task_state is TaskState.COMPLETED
    assert record.failure is None


def test_the_implementation_node_gets_its_own_worktree(harness) -> None:
    runner, claude, codex, _ = harness
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.worktree is not None
    worktree_path = Path(record.worktree["path"])
    assert codex.requests[0].workspace == worktree_path
    assert worktree_path != runner.config.repository
    # The analysis node stayed on the primary checkout, read-only.
    assert claude.requests[0].workspace == runner.config.repository
    assert claude.requests[0].filesystem_access == "read_only"
    assert codex.requests[0].filesystem_access == "scoped_write"


def test_a_read_only_node_depending_on_a_write_node_reads_the_worktree(harness) -> None:
    """A reviewer pointed at the primary checkout reviews code that predates the work.

    Caught by a live run: the reviewer globbed the untouched checkout and reported on
    it, so the review gate was inspecting the wrong directory entirely.
    """
    runner, claude, codex, _ = harness
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    worktree_path = Path(record.worktree["path"])
    review_request = claude.requests[-1]

    assert review_request.workspace == worktree_path
    # It reads the worktree; it does not get write access to it.
    assert review_request.filesystem_access == "read_only"
    assert review_request.tool_access == "read"
    # The analysis node has no write dependency, so it stays on the primary checkout.
    assert claude.requests[0].workspace == runner.config.repository


def test_review_runs_in_a_fresh_session(harness) -> None:
    """session_policy: new. Review that inherits the implementer's context is not review."""
    runner, claude, codex, _ = harness
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    review_request = claude.requests[-1]
    assert review_request.resume_from is None


def test_each_worker_gets_its_own_model(harness) -> None:
    """A model name means something to one provider only.

    A live smoke test failed with `kind=model` when a single config field sent a
    Claude alias to Codex. ADR-004 keeps Model and Worker Adapter separate.
    """
    runner, claude, codex, _ = harness
    runner.config.models = {"claude_code": "sonnet"}
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    assert claude.requests[0].model == "sonnet"
    # No entry means no --model flag, so codex keeps its own default.
    assert codex.requests[0].model is None


def test_read_only_nodes_can_read_but_not_write(harness) -> None:
    """No tools at all is not read-only, it is blind.

    A live run gave the reviewer an empty tool set: it returned `verdict: pass` and
    recorded that it had nothing capable of reading the code it was reviewing.
    """
    runner, claude, codex, _ = harness
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    for request in (claude.requests[0], claude.requests[-1]):
        assert request.tool_access == "read"
        assert request.filesystem_access == "read_only"

    assert codex.requests[0].tool_access == "write"


def test_context_package_carries_prior_artifacts_not_transcripts(harness) -> None:
    runner, claude, codex, _ = harness
    claude.queue(structured_result={"plan": ["step one"]})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    package = json.loads(codex.requests[0].prompt)
    assert package["objective"] == "Add a multiply function"
    assert any("step one" in artifact for artifact in package["prior_artifacts"])
    assert any("Never push to git" in rule for rule in package["node_rules"])
    # stdout and event streams stay in the log directory.
    assert "stdout" not in codex.requests[0].prompt


def test_node_rules_are_separate_from_the_tasks_own_constraints(harness) -> None:
    """Merging them made a reviewer report the task as self-contradictory.

    It read "This node is read-only: do not modify any file" in the same list as the
    task's constraints, next to an objective asking for a new function, and filed a
    finding about the contradiction.
    """
    runner, claude, codex, _ = harness
    runner.task = runner.task.model_copy(
        update={"constraints": ["Keep the existing add() function unchanged."]}
    )
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    review_package = json.loads(claude.requests[-1].prompt)
    write_package = json.loads(codex.requests[0].prompt)

    # The task's own constraint reaches every node, unchanged and unpadded.
    assert review_package["constraints"] == ["Keep the existing add() function unchanged."]
    assert write_package["constraints"] == ["Keep the existing add() function unchanged."]

    # Per-node rules differ by node and never leak into `constraints`.
    assert any("read-only" in rule for rule in review_package["node_rules"])
    assert any("Write only inside" in rule for rule in write_package["node_rules"])
    assert not any("read-only" in c for c in review_package["constraints"])


# ---------------------------------------------------------------------------
# Agent profiles
# ---------------------------------------------------------------------------


def _profiles(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "prompts"
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "system.md").write_text(f"You are {name}.", encoding="utf-8")
    return root


def test_each_node_gets_its_agent_profile_prompt(harness, tmp_path: Path) -> None:
    """agent_profile was declared on every node and read by nobody."""
    runner, claude, codex, _ = harness
    runner.config.prompt_root = _profiles(
        tmp_path, "task-analyst", "code-implementer", "independent-reviewer"
    )
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    assert claude.requests[0].system_prompt == "You are task-analyst."
    assert codex.requests[0].system_prompt == "You are code-implementer."
    assert claude.requests[-1].system_prompt == "You are independent-reviewer."


def test_a_missing_profile_prompt_stops_the_run_before_it_starts(harness, tmp_path: Path) -> None:
    """Running a node without its role definition is a silent quality loss."""
    runner, _, _, _ = harness
    runner.config.prompt_root = _profiles(tmp_path, "task-analyst")

    with pytest.raises(WorkflowRunError, match="no system prompt"):
        runner.create_run("RUN-1")


def test_no_prompt_root_means_no_system_prompt(harness) -> None:
    runner, claude, _, _ = harness
    claude.queue(structured_result={"plan": []})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))

    assert claude.requests[0].system_prompt is None


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------


def test_nodes_with_a_published_contract_get_it_as_an_output_schema(harness) -> None:
    """Without this the review verdict is prose, and the gate cannot read it."""
    runner, claude, codex, _ = harness
    runner.config.contracts_root = Path(__file__).resolve().parent.parent / "contracts"
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    assert claude.requests[0].output_schema_path.name == "analysis-result.schema.json"
    assert codex.requests[0].output_schema_path.name == "worker-result.schema.json"
    assert claude.requests[-1].output_schema_path.name == "review-result.schema.json"


# ---------------------------------------------------------------------------
# Artifact identity
# ---------------------------------------------------------------------------


def _run_to_completion(runner, store) -> None:
    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))


def test_the_runner_overwrites_identity_fields_the_worker_got_wrong(harness) -> None:
    """A live run had Codex report `"worker": "/root"`. It cannot know; the runner can."""
    runner, claude, codex, store = harness
    runner.config.contracts_root = CONTRACTS
    claude.queue(structured_result={"plan": [], "task_id": "TASK-999"})
    codex.queue(
        structured_result={
            "status": "completed",
            "worker": "/root",
            "task_id": "TASK-999",
            "run_id": "RUN-WHATEVER",
        }
    )
    claude.queue(structured_result={"verdict": "pass"})

    _run_to_completion(runner, store)

    implemented = store.read_artifact("RUN-1", "implement-worker-result.json")
    assert implemented["worker"] == "codex"
    assert implemented["task_id"] == "TASK-001"
    assert implemented["run_id"] == "RUN-1"


def test_a_corrected_identity_is_recorded_rather_than_silently_replaced(harness) -> None:
    runner, claude, codex, store = harness
    runner.config.contracts_root = CONTRACTS
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed", "worker": "/root"})
    claude.queue(structured_result={"verdict": "pass"})

    _run_to_completion(runner, store)

    corrections = [
        event for event in store.read_events("RUN-1") if event.kind == "identity_corrected"
    ]
    claimed = [event.detail["claimed"] for event in corrections]
    assert {"worker": "/root"} in claimed


def test_an_identity_field_the_contract_omits_is_not_invented(harness) -> None:
    """analysis-result declares task_id and no run_id. Stamping one in would be drift."""
    runner, claude, codex, store = harness
    runner.config.contracts_root = CONTRACTS
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    _run_to_completion(runner, store)

    analysis = store.read_artifact("RUN-1", "analyze-analysis.json")
    assert analysis["task_id"] == "TASK-001"
    assert "run_id" not in analysis
    assert "worker" not in analysis


def test_without_a_contract_only_fields_the_worker_supplied_are_corrected(harness) -> None:
    """Unknown shape, so a wrong value is fixed but no key is added to it."""
    runner, claude, codex, store = harness
    assert runner.config.contracts_root is None
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed", "worker": "/root"})
    claude.queue(structured_result={"verdict": "pass"})

    _run_to_completion(runner, store)

    implemented = store.read_artifact("RUN-1", "implement-worker-result.json")
    assert implemented["worker"] == "codex"
    assert "task_id" not in implemented
    assert "run_id" not in implemented


def test_a_correct_identity_produces_no_correction_event(harness) -> None:
    runner, claude, codex, store = harness
    runner.config.contracts_root = CONTRACTS
    claude.queue(structured_result={"plan": [], "task_id": "TASK-001"})
    codex.queue(
        structured_result={
            "status": "completed",
            "worker": "codex",
            "task_id": "TASK-001",
            "run_id": "RUN-1",
        }
    )
    claude.queue(
        structured_result={"verdict": "pass", "task_id": "TASK-001", "run_id": "RUN-1"}
    )

    _run_to_completion(runner, store)

    assert "identity_corrected" not in _kinds(store, "RUN-1")


# ---------------------------------------------------------------------------
# Trust boundaries
# ---------------------------------------------------------------------------


def test_verification_is_rerun_and_can_contradict_the_worker(tmp_path, harness) -> None:
    """claimed_passed is recorded, then ignored in favour of a real command."""
    runner, claude, codex, store = harness
    runner.config.verification_commands = [
        VerificationCommand(
            args=["git", "rev-parse", "--verify", "no-such-ref"], timeout_seconds=60
        )
    ]
    claude.queue(structured_result={"plan": []})
    codex.queue(
        structured_result={
            "status": "completed",
            "verification": {"claimed_passed": True, "commands": ["pytest"]},
        }
    )

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert "verification_finished" in _kinds(store, "RUN-1")
    assert record.repair_rounds >= 1
    assert record.task_state is not TaskState.COMPLETED


def test_the_verification_artifact_conforms_to_its_published_contract(harness) -> None:
    """The orchestrator produces this one itself, so nothing else will catch drift."""
    import jsonschema

    runner, claude, codex, store = harness
    contract = json.loads(
        (Path(__file__).resolve().parent.parent / "contracts" / "verification-result.schema.json")
        .read_text(encoding="utf-8")
    )
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    jsonschema.validate(store.read_artifact("RUN-1", record.artifacts["verify"]), contract)


def test_the_unverified_artifact_also_conforms(harness) -> None:
    """The "nothing was proven" shape must be a valid verification result too."""
    import jsonschema

    runner, claude, codex, store = harness
    runner.config.verification_commands = []
    runner.workflow = runner.workflow.model_copy(update={"max_repair_rounds": 0})
    contract = json.loads(
        (Path(__file__).resolve().parent.parent / "contracts" / "verification-result.schema.json")
        .read_text(encoding="utf-8")
    )
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    # The node failed, so no artifact was recorded; validate the payload it produced.
    outcome = asyncio.run(
        runner._run_verification(store.load("RUN-1"), runner.workflow.node("verify"))
    )
    jsonschema.validate(outcome.artifact, contract)
    assert outcome.artifact["verified"] is False


def test_a_run_with_no_verification_commands_fails_rather_than_passes(harness) -> None:
    """Nothing proven must not read the same as everything passed."""
    runner, claude, codex, store = harness
    runner.config.verification_commands = []
    runner.workflow = runner.workflow.model_copy(update={"max_repair_rounds": 0})
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.task_state is TaskState.FAILED_PERMANENT
    assert "verification_skipped" in _kinds(store, "RUN-1")


def test_a_worker_writing_outside_its_worktree_fails_the_run(harness, repository) -> None:
    """Containment outranks whatever the worker reported about itself."""
    runner, claude, codex, store = harness
    runner.workflow = runner.workflow.model_copy(update={"max_repair_rounds": 0})
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    codex.escape_write = repository / "PLANTED.txt"

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.task_state is TaskState.FAILED_PERMANENT
    assert "containment_violation" in _kinds(store, "RUN-1")
    assert "implement" not in record.completed_nodes


def test_a_failing_worker_does_not_advance_the_graph(harness) -> None:
    runner, claude, _, _ = harness
    runner.workflow = runner.workflow.model_copy(update={"max_repair_rounds": 0})
    claude.queue(reported_error=True, exit_code=1, error_kind="auth", structured_result={})

    runner.create_run("RUN-1")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.task_state is TaskState.FAILED_PERMANENT
    assert "auth" in (record.failure or "")
    assert record.completed_nodes == []


# ---------------------------------------------------------------------------
# Repair loop
# ---------------------------------------------------------------------------


def test_a_review_requesting_changes_replays_the_implementation(harness) -> None:
    runner, claude, codex, store = harness
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "fail", "findings": ["missing test"]})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.repair_rounds == 1
    assert "repair_started" in _kinds(store, "RUN-1")
    assert record.pending_approval.approval_type == "final_result"
    # The implementation ran twice, in the same worktree.
    assert len(codex.requests) == 2
    assert codex.requests[0].workspace == codex.requests[1].workspace


def test_repair_is_bounded(harness) -> None:
    runner, claude, codex, store = harness
    runner.workflow = runner.workflow.model_copy(update={"max_repair_rounds": 1})
    claude.queue(structured_result={"plan": []})
    for _ in range(4):
        codex.queue(structured_result={"status": "completed"})
        claude.queue(structured_result={"verdict": "fail"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.repair_rounds == 1
    assert record.task_state is TaskState.FAILED_PERMANENT
    assert "repair budget is spent" in (record.failure or "")


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


def test_rejecting_cancels_the_run(harness) -> None:
    runner, claude, _, _ = harness
    claude.queue(structured_result={"plan": []})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    record = runner.decide("RUN-1", "reject", reason="wrong approach")

    assert record.task_state is TaskState.CANCELLED
    assert record.failure == "wrong approach"


def test_requesting_changes_at_a_gate_starts_a_repair_round(harness) -> None:
    runner, claude, _, store = harness
    claude.queue(structured_result={"plan": []})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    record = runner.decide("RUN-1", "request_changes", reason="narrow the scope")

    assert record.repair_rounds == 1
    assert record.task_state is TaskState.READY
    assert "approval_decided" in _kinds(store, "RUN-1")


def test_deciding_without_a_pending_approval_is_refused(harness) -> None:
    runner, _, _, _ = harness
    runner.create_run("RUN-1")

    with pytest.raises(WorkflowRunError, match="not waiting for a decision"):
        runner.decide("RUN-1", "approve")


def test_an_unknown_decision_is_refused(harness) -> None:
    runner, claude, _, _ = harness
    claude.queue(structured_result={"plan": []})
    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))

    with pytest.raises(WorkflowRunError, match="unknown decision"):
        runner.decide("RUN-1", "looks fine to me")


def test_the_approval_package_distinguishes_commits_from_dirty_files(harness) -> None:
    """A branch name in `changes` reads as "there are commits"; a live run had none."""
    runner, claude, codex, _ = harness
    codex.writes = {"new_file.py": "print('work')\n"}
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    changes = record.pending_approval.changes
    assert any("uncommitted change" in entry for entry in changes), changes


def test_a_terminal_run_releases_its_worktree(harness) -> None:
    """A lock left behind makes reconcile() report a finished run as live forever."""
    runner, claude, codex, store = harness
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))
    record = runner.decide("RUN-1", "approve")

    assert record.task_state is TaskState.COMPLETED
    assert "worktree_released" in _kinds(store, "RUN-1")
    # The worktree survives - the branch is the deliverable - but is no longer locked.
    assert runner.worktrees.reconcile().locked == ()
    assert Path(record.worktree["path"]).is_dir()


def test_a_cancelled_run_also_releases_its_worktree(harness) -> None:
    runner, claude, codex, store = harness
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "reject", reason="not what I wanted")

    assert runner.worktrees.reconcile().locked == ()


def test_the_approval_package_is_written_as_an_artifact(harness) -> None:
    runner, claude, _, store = harness
    claude.queue(structured_result={"plan": []})
    runner.create_run("RUN-1")
    record = asyncio.run(runner.advance("RUN-1"))

    payload = store.read_artifact("RUN-1", f"{record.pending_approval.approval_id}.json")

    # Must match contracts/approval-package.schema.json, which has no such field.
    assert "resume_after_node" not in payload
    assert payload["approval_type"] == "plan"
    assert payload["risk_level"] in {"low", "medium", "high"}


# ---------------------------------------------------------------------------
# Resumability
# ---------------------------------------------------------------------------


def test_a_run_can_be_continued_by_a_new_runner_instance(harness, repository, task) -> None:
    """Everything needed to continue lives in the record, not in the process."""
    runner, claude, codex, store = harness
    claude.queue(structured_result={"plan": []})
    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))

    revived = WorkflowRunner(
        workflow=runner.workflow,
        task=task,
        config=runner.config,
        adapters=runner.adapters,
        store=FilesystemRunStore(runner.config.run_root),
    )
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    revived.decide("RUN-1", "approve")
    record = asyncio.run(revived.advance("RUN-1"))

    assert record.completed_nodes == ["analyze", "implement", "verify", "review"]


def test_advancing_a_paused_run_does_nothing(harness) -> None:
    runner, claude, _, _ = harness
    claude.queue(structured_result={"plan": []})
    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))

    again = asyncio.run(runner.advance("RUN-1"))

    assert again.completed_nodes == ["analyze"]
    assert again.pending_approval is not None


def test_the_event_log_records_the_decisions_that_were_made(harness) -> None:
    runner, claude, codex, store = harness
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    kinds = _kinds(store, "RUN-1")
    for expected in (
        "run_created",
        "node_started",
        "worktree_created",
        "containment_armed",
        "verification_finished",
        "approval_requested",
        "approval_decided",
        "state_changed",
    ):
        assert expected in kinds, expected
