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
from orchestrator.workflow.runner import (
    MAX_LISTED_CHANGES,
    RunnerConfig,
    WorkflowRunError,
    WorkflowRunner,
)
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
        #: When set, collect() blocks on it, so a test can cancel mid-worker.
        self.hold: asyncio.Event | None = None
        self.cancelled: list[WorkerHandle] = []

    def queue(self, **payload) -> None:
        self.outcomes.append(payload)

    async def health_check(self) -> dict:
        return {"worker": self.name, "available": True}

    async def start(self, request: WorkerRequest) -> WorkerHandle:
        self.requests.append(request)
        self._counter += 1
        for name, content in self.writes.items():
            destination = request.workspace / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        if self.escape_write is not None:
            self.escape_write.write_text("ESCAPED", encoding="utf-8")
        return WorkerHandle(worker_run_id=f"{self.name}-{self._counter}", process_id=1000)

    def stream_events(self, handle: WorkerHandle) -> AsyncIterator[str]:
        async def _empty() -> AsyncIterator[str]:
            return
            yield ""  # pragma: no cover

        return _empty()

    async def cancel(self, handle: WorkerHandle) -> None:
        self.cancelled.append(handle)
        if self.hold is not None:
            # A real adapter kills the process, which unblocks the collect() waiting
            # on it. Releasing the event is this fake's equivalent.
            self.hold.set()

    async def collect(self, handle: WorkerHandle) -> WorkerResult:
        if self.hold is not None:
            await self.hold.wait()
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


def _run_to_completion(runner):
    """Drive RUN-1 through the plan gate to the final one, and return where it stopped."""
    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    return asyncio.run(runner.advance("RUN-1"))


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

    _run_to_completion(runner)

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

    _run_to_completion(runner)

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

    _run_to_completion(runner)

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

    _run_to_completion(runner)

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

    _run_to_completion(runner)

    assert "identity_corrected" not in _kinds(store, "RUN-1")


# ---------------------------------------------------------------------------
# Trust boundaries
# ---------------------------------------------------------------------------


def test_verification_is_rerun_and_can_contradict_the_worker(tmp_path, harness) -> None:
    """claimed_passed is recorded, then ignored in favour of a real command.

    The command passes on the base revision and fails once the worker has edited a
    tracked file, so this is a regression the run must not survive.
    """
    runner, claude, codex, store = harness
    runner.config.verification_commands = [
        VerificationCommand(args=["git", "diff", "--exit-code"], timeout_seconds=60)
    ]
    codex.writes = {"calc.py": "def add(a, b):\n    return a - b\n"}
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


def test_a_command_already_failing_on_the_base_revision_is_not_this_runs_fault(
    harness,
) -> None:
    """A real project run failed this way: correct work, a suite that was already red."""
    runner, claude, codex, store = harness
    runner.config.verification_commands = [
        VerificationCommand(
            args=["git", "rev-parse", "--verify", "no-such-ref"], timeout_seconds=60
        )
    ]
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    kinds = _kinds(store, "RUN-1")
    assert "baseline_started" in kinds and "baseline_finished" in kinds
    assert record.verification_baseline is not None
    assert record.verification_baseline[0] != 0

    artifact = store.read_artifact("RUN-1", record.artifacts["verify"])
    assert artifact["verified"] is True
    assert artifact["regressions"] == []
    # The command still failed. "verified" must not be read as "the suite is green".
    assert artifact["commands"][0]["exit_code"] != 0
    assert "no regression" in artifact["reason"]
    # The run was not sent round the repair loop for a failure it did not cause.
    assert record.repair_rounds == 0


def test_the_baseline_is_taken_once_and_reused(harness) -> None:
    """The base revision does not move, and on a real project it costs a full suite."""
    runner, claude, codex, store = harness
    runner.config.verification_commands = [
        VerificationCommand(args=["git", "diff", "--exit-code"], timeout_seconds=60)
    ]
    codex.writes = {"calc.py": "def add(a, b):\n    return a - b\n"}
    claude.queue(structured_result={"plan": []})
    for _ in range(4):
        codex.queue(structured_result={"status": "completed"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    starts = [k for k in _kinds(store, "RUN-1") if k == "baseline_started"]
    assert record.repair_rounds >= 1, "the run should have gone round more than once"
    assert len(starts) == 1, f"baseline taken {len(starts)} times"


def test_a_failure_with_no_baseline_stays_a_failure(harness) -> None:
    """Nothing known means no excuse. The safe reading of a failure is a failure."""
    runner, claude, codex, store = harness
    runner.config.verification_commands = [
        VerificationCommand(
            args=["git", "rev-parse", "--verify", "no-such-ref"], timeout_seconds=60
        )
    ]

    async def no_baseline(record, node):
        return None

    runner._verification_baseline = no_baseline  # type: ignore[method-assign]
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.repair_rounds >= 1
    assert record.task_state is not TaskState.COMPLETED


def test_the_baseline_worktree_does_not_outlive_the_check(harness, tmp_path) -> None:
    """It is a scratch checkout, not a deliverable; leaving it strands a locked tree."""
    runner, claude, codex, store = harness
    runner.config.verification_commands = [
        VerificationCommand(
            args=["git", "rev-parse", "--verify", "no-such-ref"], timeout_seconds=60
        )
    ]
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    assert "baseline_finished" in _kinds(store, "RUN-1")
    assert not (tmp_path / "workspaces" / "RUN-1-baseline").exists()


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


def test_a_repair_names_the_node_that_asked_for_it(harness) -> None:
    """A live run recorded "review requested changes" for a blocked implementer."""
    runner, claude, codex, store = harness
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "blocked", "summary": "cannot reach the network"})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    asyncio.run(runner.advance("RUN-1"))

    repairs = [e for e in store.read_events("RUN-1") if e.kind == "repair_started"]
    assert repairs, _kinds(store, "RUN-1")
    reason = repairs[0].detail["reason"]
    assert "implement" in reason and "blocked" in reason, reason
    # The review node never ran, so nothing may claim it asked for anything.
    assert "review" not in reason, reason


def test_a_blocked_worker_that_exhausts_the_budget_fails_for_the_right_reason(
    harness,
) -> None:
    runner, claude, codex, store = harness
    runner.workflow = runner.workflow.model_copy(update={"max_repair_rounds": 1})
    claude.queue(structured_result={"plan": []})
    for _ in range(4):
        codex.queue(structured_result={"status": "blocked"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert record.task_state is TaskState.FAILED_PERMANENT
    assert "implement reported it was blocked" in (record.failure or "")
    assert "repair budget is spent" in (record.failure or "")
    assert "review" not in (record.failure or "")


def test_a_review_verdict_still_names_the_review(harness) -> None:
    """The other path through the same branch must not lose its own wording."""
    runner, claude, codex, store = harness
    runner.workflow = runner.workflow.model_copy(update={"max_repair_rounds": 0})
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "request_changes"})

    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.decide("RUN-1", "approve")
    record = asyncio.run(runner.advance("RUN-1"))

    assert "review returned request_changes" in (record.failure or "")


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was never reached")
        await asyncio.sleep(0.01)


def test_cancelling_reaches_the_worker_that_is_running(harness) -> None:
    """Without killing the process a cancel would only land when the node ended."""
    runner, claude, _, store = harness
    claude.hold = asyncio.Event()
    claude.queue(structured_result={"plan": []})

    async def scenario():
        runner.create_run("RUN-1")
        advancing = asyncio.create_task(runner.advance("RUN-1"))
        await _wait_for(lambda: len(claude.requests) == 1)
        await runner.request_cancel(reason="operator stopped it")
        return await advancing

    record = asyncio.run(scenario())

    assert record.task_state is TaskState.CANCELLED
    assert record.failure == "operator stopped it"
    assert claude.cancelled, "the running worker was never cancelled"


def test_a_cancelled_node_does_not_spend_a_repair_round(harness) -> None:
    """A cancelled worker exits non-zero, which looks like a failure worth retrying."""
    runner, claude, _, store = harness
    claude.hold = asyncio.Event()
    claude.queue(exit_code=1, reported_error=True, error_kind="cancelled")

    async def scenario():
        runner.create_run("RUN-1")
        advancing = asyncio.create_task(runner.advance("RUN-1"))
        await _wait_for(lambda: len(claude.requests) == 1)
        await runner.request_cancel()
        return await advancing

    record = asyncio.run(scenario())

    assert record.task_state is TaskState.CANCELLED
    assert record.repair_rounds == 0
    assert "repair_started" not in _kinds(store, "RUN-1")
    # One attempt, not a retry of the node the operator just stopped.
    assert len(claude.requests) == 1


def test_cancelling_a_paused_run_needs_no_worker(harness) -> None:
    runner, claude, _, store = harness
    claude.queue(structured_result={"plan": []})
    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))

    record = runner.cancel("RUN-1", reason="not worth continuing")

    assert record.task_state is TaskState.CANCELLED
    assert record.failure == "not worth continuing"
    assert record.pending_approval is None


def test_cancelling_a_finished_run_is_refused(harness) -> None:
    runner, claude, _, _ = harness
    claude.queue(structured_result={"plan": []})
    runner.create_run("RUN-1")
    asyncio.run(runner.advance("RUN-1"))
    runner.cancel("RUN-1")

    with pytest.raises(WorkflowRunError, match="has finished as CANCELLED"):
        runner.cancel("RUN-1")


def test_cancelling_mid_worker_releases_the_worktree(harness) -> None:
    """The implement node holds a locked worktree; a cancel must not strand it."""
    runner, claude, codex, store = harness
    codex.hold = asyncio.Event()
    claude.queue(structured_result={"plan": []})

    async def scenario():
        runner.create_run("RUN-1")
        await runner.advance("RUN-1")
        runner.decide("RUN-1", "approve")
        advancing = asyncio.create_task(runner.advance("RUN-1"))
        await _wait_for(lambda: len(codex.requests) == 1)
        await runner.request_cancel()
        return await advancing

    record = asyncio.run(scenario())

    assert record.task_state is TaskState.CANCELLED
    assert "worktree_released" in _kinds(store, "RUN-1")


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


def test_the_approval_package_names_the_dirty_files(harness) -> None:
    """A count alone is not something a human can decide on."""
    runner, claude, codex, _ = harness
    codex.writes = {"new_file.py": "print('work')\n"}
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    record = _run_to_completion(runner)

    changes = record.pending_approval.changes
    assert any("new_file.py" in entry for entry in changes), changes


def test_the_named_files_expose_noise_the_count_hid(harness) -> None:
    """The first live run's "3 uncommitted change(s)" was one file and two caches."""
    runner, claude, codex, _ = harness
    codex.writes = {
        "slugify.py": "def slugify(text): return text\n",
        "__pycache__/slugify.cpython-312.pyc": "x",
        "tests/__pycache__/test_slugify.cpython-312.pyc": "x",
    }
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    record = _run_to_completion(runner)

    changes = record.pending_approval.changes
    named = " ".join(changes)
    assert "slugify.py" in named, changes
    # The caches are still reported - hiding a real write to make a number look
    # tidier is the worse failure - but a reader can now tell them apart.
    assert "__pycache__" in named, changes


def test_a_long_change_list_is_summarised_rather_than_dumped(harness) -> None:
    runner, claude, codex, _ = harness
    codex.writes = {f"file_{index:03d}.py": "x\n" for index in range(MAX_LISTED_CHANGES + 5)}
    claude.queue(structured_result={"plan": []})
    codex.queue(structured_result={"status": "completed"})
    claude.queue(structured_result={"verdict": "pass"})

    record = _run_to_completion(runner)

    changes = record.pending_approval.changes
    listed = [entry for entry in changes if ".py" in entry]
    assert len(listed) == MAX_LISTED_CHANGES, len(listed)
    assert any("and 5 more" in entry for entry in changes), changes


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
