"""Builds runners from configuration and supervises advancing runs.

The HTTP layer must not block on a run: advancing one drives real CLI workers and
takes minutes. So a request starts or resumes a run and returns immediately, and the
caller polls the run record. This service owns that split, plus the two things that
follow from it — knowing which runs are already moving, and rebuilding a runner for a
run this process did not start.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from apps.api.app.config import Settings, get_settings
from execution.verifier import VerificationCommand
from orchestrator.domain.models import Task
from orchestrator.workflow import (
    RunnerConfig,
    RunRecord,
    RunStore,
    WorkflowDefinition,
    WorkflowRunner,
    load_workflow,
)
from workers import ADAPTERS_BY_NAME


class OrchestrationError(RuntimeError):
    """The service cannot honour the request as configured."""


class NotConfigured(OrchestrationError):
    """Runs are impossible with the current settings, and it is not the caller's fault."""


class OrchestrationService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = RunStore(self.settings.run_root)
        self._workflow: WorkflowDefinition | None = None
        #: One lock per run. A run advancing twice at once would have two workers in
        #: the same worktree and two writers to the same record.
        self._locks: dict[str, asyncio.Lock] = {}
        self._advancing: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    # -- configuration -----------------------------------------------------------

    @property
    def workflow(self) -> WorkflowDefinition:
        if self._workflow is None:
            self._workflow = load_workflow(self.settings.workflow_path)
        return self._workflow

    def require_project_root(self) -> Path:
        if self.settings.project_root is None:
            raise NotConfigured(
                "PROJECT_ROOT is not set, so there is no repository to run against"
            )
        if not (self.settings.project_root / ".git").exists():
            raise NotConfigured(
                f"PROJECT_ROOT is not a git repository: {self.settings.project_root}"
            )
        return self.settings.project_root

    def runner_config(self) -> RunnerConfig:
        return RunnerConfig(
            repository=self.require_project_root(),
            workspace_root=self.settings.worktree_root,
            run_root=self.settings.run_root,
            verification_commands=[
                VerificationCommand(args=list(argv), timeout_seconds=600)
                for argv in self.settings.verification_commands
            ],
            node_timeout_seconds=self.settings.default_task_timeout_seconds,
            models=dict(self.settings.worker_models),
            prompt_root=self.settings.prompt_root,
            contracts_root=self.settings.contracts_root,
        )

    def runner_for(self, task: Task) -> WorkflowRunner:
        return WorkflowRunner(
            workflow=self.workflow,
            task=task,
            config=self.runner_config(),
            adapters={name: adapter() for name, adapter in ADAPTERS_BY_NAME.items()},
            store=self.store,
        )

    def runner_for_run(self, record: RunRecord) -> WorkflowRunner:
        """Rebuild a runner from a stored record, for a run this process did not start."""
        if not record.task:
            raise OrchestrationError(
                f"run {record.run_id} predates task storage and cannot be resumed"
            )
        return self.runner_for(Task.model_validate(record.task))

    # -- lifecycle ---------------------------------------------------------------

    def create_run(self, task: Task) -> RunRecord:
        return self.runner_for(task).create_run()

    def get(self, run_id: str) -> RunRecord:
        return self.store.load(run_id)

    def list_runs(self, limit: int = 50) -> tuple[RunRecord, ...]:
        return self.store.list_runs()[:limit]

    def is_advancing(self, run_id: str) -> bool:
        return run_id in self._advancing

    def start_advance(self, run_id: str) -> bool:
        """Advance a run in the background. False when it is already moving."""
        if run_id in self._advancing:
            return False
        self._advancing.add(run_id)
        task = asyncio.create_task(self._advance(run_id))
        # Hold a reference: a task only referenced by the event loop can be collected
        # mid-flight, which would abandon a run with a live worker attached.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _advance(self, run_id: str) -> None:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        try:
            async with lock:
                record = self.store.load(run_id)
                await self.runner_for_run(record).advance(run_id)
        except Exception as exc:  # noqa: BLE001 - recorded on the run, not raised at nobody
            self._record_service_failure(run_id, exc)
        finally:
            self._advancing.discard(run_id)

    def _record_service_failure(self, run_id: str, exc: Exception) -> None:
        """A background failure has no caller to raise at, so it goes on the run."""
        from orchestrator.workflow.store import RunEvent

        self.store.append_event(
            run_id,
            RunEvent(kind="advance_failed", detail={"error": f"{type(exc).__name__}: {exc}"}),
        )
        try:
            record = self.store.load(run_id)
        except Exception:  # noqa: BLE001 - the record itself is unreadable
            return
        record.failure = f"{type(exc).__name__}: {exc}"
        self.store.save(record)

    def decide(self, run_id: str, decision: str, reason: str) -> RunRecord:
        if run_id in self._advancing:
            raise OrchestrationError(f"run {run_id} is advancing; decide when it pauses")
        record = self.store.load(run_id)
        return self.runner_for_run(record).decide(run_id, decision, reason=reason)

    # -- reads -------------------------------------------------------------------

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return [event.model_dump() for event in self.store.read_events(run_id)]

    def artifact(self, run_id: str, name: str) -> Any:
        record = self.store.load(run_id)
        known = set(record.artifacts.values())
        if record.pending_approval is not None:
            known.add(f"{record.pending_approval.approval_id}.json")
        if name not in known:
            # Serving an arbitrary filename from the run directory would turn this
            # endpoint into a file reader for anything the path can reach.
            raise OrchestrationError(f"run {run_id} has no artifact named {name}")
        return self.store.read_artifact(run_id, name)


_service: OrchestrationService | None = None


def get_service() -> OrchestrationService:
    """FastAPI dependency. One instance, so the advancing-run set is shared."""
    global _service
    if _service is None:
        _service = OrchestrationService()
    return _service


def reset_service() -> None:
    """Drop the cached service. For tests that change settings."""
    global _service
    _service = None
