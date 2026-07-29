"""Task intake and run control over HTTP.

The service is pointed at a real disposable repository and a real RunStore; only the
workers are scripted. What matters here is the HTTP contract - what a caller can
submit, observe and decide, and what it is refused.
"""

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app.config import Settings
from apps.api.app.main import app
from apps.api.app.services.orchestration import OrchestrationService, get_service
from workers.base import (
    WorkerAdapter,
    WorkerCapabilities,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

TASK = {
    "task_id": "TASK-001",
    "project_id": "PROJ-001",
    "task_type": "feature",
    "objective": "Add a multiply function",
    "acceptance_criteria": ["multiply(2, 3) returns 6"],
    "permissions": {"filesystem": "scoped_write"},
    "expected_outputs": ["calc.py"],
}

GRAPH = {
    "workflow_id": "api-slice",
    "version": "0.1.0",
    "max_repair_rounds": 1,
    "nodes": [
        {
            "id": "analyze",
            "agent_profile": "task-analyst",
            "worker_requirement": "claude_code",
            "expected_artifact": "analysis.json",
            "approval_after": "plan",
        },
        {
            "id": "sign_off",
            "agent_profile": "human-approval",
            "worker_requirement": "human",
            "depends_on": ["analyze"],
            "expected_artifact": "approval-decision.json",
        },
    ],
}


class ScriptedAdapter(WorkerAdapter):
    capabilities = WorkerCapabilities(
        structured_output=True,
        stream_events=True,
        resume_session=True,
        cancel_process=True,
        scoped_write=False,
        server_mode=False,
    )

    def __init__(self, name: str = "claude_code") -> None:
        self.name = name
        self._counter = 0

    async def health_check(self) -> dict:
        return {"worker": self.name, "available": True}

    async def start(self, request: WorkerRequest) -> WorkerHandle:
        self._counter += 1
        self._log_dir = request.log_dir
        return WorkerHandle(worker_run_id=f"{self.name}-{self._counter}", process_id=1)

    def stream_events(self, handle: WorkerHandle) -> AsyncIterator[str]:
        async def _empty() -> AsyncIterator[str]:
            return
            yield ""  # pragma: no cover

        return _empty()

    async def cancel(self, handle: WorkerHandle) -> None:
        return None

    async def collect(self, handle: WorkerHandle) -> WorkerResult:
        path = self._log_dir / f"{handle.worker_run_id}.outcome.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"structured_result": {"plan": ["add multiply"]}}), encoding="utf-8"
        )
        return WorkerResult(
            exit_code=0,
            stdout_path=path,
            stderr_path=path,
            result_path=path,
            session_id="session-1",
        )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=t@l", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=t@l", "-c", "user.name=t", "commit", "-q", "-m", "base"],
    ):
        subprocess.run(argv, cwd=str(repo), capture_output=True, check=True)
    return repo


@pytest.fixture
def client(tmp_path: Path, project: Path):
    """A client whose service runs scripted workers against a throwaway project."""
    workflow_path = tmp_path / "workflow.yaml"
    workflow_path.write_text(json.dumps(GRAPH), encoding="utf-8")  # YAML is a JSON superset

    settings = Settings(
        run_root=tmp_path / "runs",
        worktree_root=tmp_path / "workspaces",
        project_root=project,
        workflow_path=workflow_path,
        prompt_root=REPO_ROOT / "prompts",
        contracts_root=REPO_ROOT / "contracts",
    )
    (tmp_path / "runs").mkdir()
    (tmp_path / "workspaces").mkdir()

    service = OrchestrationService(settings)
    original = service.runner_for

    def _scripted(task):
        runner = original(task)
        runner.adapters = {"claude_code": ScriptedAdapter()}
        return runner

    service.runner_for = _scripted  # type: ignore[method-assign]

    app.dependency_overrides[get_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client, service
    app.dependency_overrides.clear()


def _submit(test_client, start: bool = True):
    return test_client.post("/api/v1/tasks", json={"task": TASK, "start": start})


def _wait_for_pause(test_client, run_id: str, timeout: float = 30.0) -> dict:
    """Poll until the background advance settles. Mirrors what a caller must do."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = test_client.get(f"/api/v1/runs/{run_id}").json()
        if not payload["advancing"]:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never stopped advancing")


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


def test_submitting_a_task_opens_a_run(client) -> None:
    test_client, _ = client
    response = _submit(test_client, start=False)

    assert response.status_code == 201
    payload = response.json()
    assert payload["task_id"] == "TASK-001"
    assert payload["task_state"] == "PENDING"
    assert payload["completed_nodes"] == []


def test_an_invalid_task_contract_is_refused(client) -> None:
    test_client, _ = client
    broken = {**TASK, "acceptance_criteria": []}  # min_length=1

    response = test_client.post("/api/v1/tasks", json={"task": broken, "start": False})

    assert response.status_code == 422


def test_the_task_is_stored_so_a_later_process_can_resume(client) -> None:
    """Without this a run only survives while its caller holds the Task object."""
    test_client, service = client
    run_id = _submit(test_client, start=False).json()["run_id"]

    record = service.store.load(run_id)

    assert record.task["task_id"] == "TASK-001"
    assert service.runner_for_run(record).task.objective == "Add a multiply function"


def test_runs_cannot_start_without_a_project_root(tmp_path: Path) -> None:
    """Misconfiguration is the service's fault, not the caller's."""
    settings = Settings(run_root=tmp_path / "runs", project_root=None)
    (tmp_path / "runs").mkdir()
    app.dependency_overrides[get_service] = lambda: OrchestrationService(settings)
    try:
        with TestClient(app) as test_client:
            response = test_client.post("/api/v1/tasks", json={"task": TASK})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "PROJECT_ROOT" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Advancing and pausing
# ---------------------------------------------------------------------------


def test_a_started_run_reaches_the_approval_gate(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]

    payload = _wait_for_pause(test_client, run_id)

    assert payload["task_state"] == "WAITING_APPROVAL"
    assert payload["awaiting_decision"] == "plan"
    assert payload["completed_nodes"] == ["analyze"]


def test_advance_returns_before_the_run_finishes(client) -> None:
    """The caller polls; it does not hold a request open for minutes."""
    test_client, _ = client
    response = _submit(test_client)

    assert response.status_code == 201
    # Reported immediately, before any node has run.
    assert response.json()["completed_nodes"] == []
    _wait_for_pause(test_client, response.json()["run_id"])


def test_a_run_waiting_for_a_decision_refuses_to_advance(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)

    response = test_client.post(f"/api/v1/runs/{run_id}/advance")

    assert response.status_code == 409
    assert "waiting for a decision" in response.json()["detail"]


def test_a_finished_run_refuses_to_advance(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)
    test_client.post(f"/api/v1/runs/{run_id}/decision", json={"decision": "reject"})

    response = test_client.post(f"/api/v1/runs/{run_id}/advance")

    assert response.status_code == 409
    assert "finished" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def test_approving_the_plan_continues_the_run_to_the_final_gate(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)

    response = test_client.post(
        f"/api/v1/runs/{run_id}/decision", json={"decision": "approve", "reason": "looks right"}
    )
    assert response.status_code == 200

    payload = _wait_for_pause(test_client, run_id)
    assert payload["awaiting_decision"] == "final_result"


def test_rejecting_cancels_the_run(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)

    payload = test_client.post(
        f"/api/v1/runs/{run_id}/decision", json={"decision": "reject", "reason": "wrong shape"}
    ).json()

    assert payload["task_state"] == "CANCELLED"
    assert payload["failure"] == "wrong shape"


def test_a_paused_run_can_be_cancelled_over_the_api(client) -> None:
    """Rejecting an approval was the only way to stop a run; it needed its own verb."""
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)

    payload = test_client.post(
        f"/api/v1/runs/{run_id}/cancel", json={"reason": "no longer needed"}
    ).json()

    assert payload["task_state"] == "CANCELLED"
    assert payload["failure"] == "no longer needed"
    assert payload["pending_approval"] is None


def test_cancelling_without_a_body_is_allowed(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)

    response = test_client.post(f"/api/v1/runs/{run_id}/cancel")

    assert response.status_code == 200
    assert response.json()["task_state"] == "CANCELLED"


def test_cancelling_a_finished_run_is_refused(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)
    test_client.post(f"/api/v1/runs/{run_id}/cancel")

    response = test_client.post(f"/api/v1/runs/{run_id}/cancel")

    assert response.status_code == 409
    assert "CANCELLED" in response.json()["detail"]


def test_cancelling_an_unknown_run_is_a_404(client) -> None:
    test_client, _ = client

    assert test_client.post("/api/v1/runs/RUN-nope/cancel").status_code == 404


def test_an_unknown_decision_is_refused(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)

    response = test_client.post(
        f"/api/v1/runs/{run_id}/decision", json={"decision": "looks fine"}
    )

    assert response.status_code == 422


def test_deciding_with_no_pending_approval_is_refused(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client, start=False).json()["run_id"]

    response = test_client.post(f"/api/v1/runs/{run_id}/decision", json={"decision": "approve"})

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_listing_runs_returns_the_newest_first(client) -> None:
    test_client, _ = client
    first = _submit(test_client, start=False).json()["run_id"]
    second = _submit(test_client, start=False).json()["run_id"]

    listed = [run["run_id"] for run in test_client.get("/api/v1/runs").json()]

    assert listed[:2] == [second, first]


def test_the_event_log_is_readable(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)

    kinds = [event["kind"] for event in test_client.get(f"/api/v1/runs/{run_id}/events").json()]

    assert kinds[0] == "run_created"
    assert "approval_requested" in kinds


def test_the_approval_package_is_served(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)

    payload = test_client.get(f"/api/v1/runs/{run_id}/approval").json()

    assert payload["approval_type"] == "plan"
    assert payload["risk_level"] in {"low", "medium", "high"}
    # An internal field must not leak into the published package shape.
    assert "resume_after_node" not in payload


def test_artifacts_are_served_by_name(client) -> None:
    test_client, _ = client
    run_id = _submit(test_client).json()["run_id"]
    _wait_for_pause(test_client, run_id)
    detail = test_client.get(f"/api/v1/runs/{run_id}").json()

    name = detail["artifacts"]["analyze"]
    payload = test_client.get(f"/api/v1/runs/{run_id}/artifacts/{name}").json()

    assert payload["plan"] == ["add multiply"]
    # Stamped by the runner, not claimed by the worker.
    assert payload["task_id"] == "TASK-001"


def test_an_unlisted_artifact_name_is_refused(client) -> None:
    """The endpoint serves this run's artifacts, not whatever a path can reach."""
    test_client, _ = client
    run_id = _submit(test_client, start=False).json()["run_id"]

    response = test_client.get(f"/api/v1/runs/{run_id}/artifacts/..%2F..%2Frun.json")

    assert response.status_code == 404


def test_an_unknown_run_is_a_404(client) -> None:
    test_client, _ = client

    assert test_client.get("/api/v1/runs/RUN-nope").status_code == 404
    assert test_client.get("/api/v1/runs/RUN-nope/events").status_code == 404
    assert test_client.post("/api/v1/runs/RUN-nope/advance").status_code == 404
