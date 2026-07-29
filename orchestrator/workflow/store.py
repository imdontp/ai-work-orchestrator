"""Run state, artifacts and the audit trail.

:class:`RunStore` is the contract; :class:`FilesystemRunStore` here and
:class:`~orchestrator.workflow.postgres_store.PostgresRunStore` implement it. The runner
never learns which one it has.

The event log is append-only and never rewritten, in either backend. A run that crashed
mid-node must leave behind what it had already established, or the restart
reconciliation in ``WorktreeManager`` has nothing to reconcile against.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from orchestrator.domain.models import TaskState


class RunStoreError(RuntimeError):
    """The run's own state is unreadable or inconsistent."""


class RunEvent(BaseModel):
    """One thing that happened, in the order it happened."""

    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    kind: str
    node_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    """A pause point. Shape follows contracts/approval-package.schema.json."""

    schema_version: str = "1.0"
    approval_id: str
    run_id: str
    approval_type: str
    risk_level: str
    summary: str
    changes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    requested_decision: str = "approve"
    external_effect: str | None = None
    #: Which node the run should continue from once a decision arrives. Not part of the
    #: published contract — stripped before the package is written as an artifact.
    resume_after_node: str = ""


class RunRecord(BaseModel):
    """Everything needed to resume a run in a later process."""

    run_id: str
    task_id: str
    #: The task contract this run was created from. Stored rather than referenced so a
    #: later process can rebuild the runner from the record alone - without it, "a run
    #: outlives the process that started it" only held for a caller that still had the
    #: Task object in hand.
    task: dict[str, Any] = Field(default_factory=dict)
    workflow_id: str
    workflow_version: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    task_state: TaskState = TaskState.PENDING
    completed_nodes: list[str] = Field(default_factory=list)
    #: Node id -> artifact filename, for the context builder.
    artifacts: dict[str, str] = Field(default_factory=dict)
    repair_rounds: int = 0
    pending_approval: ApprovalRequest | None = None
    failure: str | None = None
    #: Worker name -> session id, so a repair round can resume the right conversation.
    sessions: dict[str, str] = Field(default_factory=dict)
    #: Serialized Worktree, when one was created for this run.
    worktree: dict[str, str] | None = None
    #: Exit code per verification command on the untouched base revision, captured the
    #: first time verification fails. Absent while verification passes, because a run
    #: that never fails has no question to answer and the baseline costs a full suite.
    #: Kept on the record so repair rounds reuse it - the base revision does not move.
    verification_baseline: list[int] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.task_state in {
            TaskState.COMPLETED,
            TaskState.FAILED_PERMANENT,
            TaskState.CANCELLED,
        }


class RunStore(ABC):
    """Where a run's state, artifacts and audit trail live.

    Two implementations, one contract. `docs/SYSTEM_ARCHITECTURE.md` targets PostgreSQL
    for records, events and artifact metadata, and the local filesystem for artifact
    payloads — so both backends still hand out real directories for worker logs and
    artifact files. A worker writes files; that is not negotiable by a storage choice.
    """

    #: Root for the parts that must exist on disk whatever the backend.
    run_root: Path

    # -- layout ------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.run_root / run_id

    def artifact_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def log_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "logs"

    def artifact_path(self, run_id: str, filename: str) -> Path:
        return self.artifact_dir(run_id) / filename

    # -- record ------------------------------------------------------------------

    @abstractmethod
    def create(self, record: RunRecord) -> RunRecord: ...

    @abstractmethod
    def load(self, run_id: str) -> RunRecord: ...

    @abstractmethod
    def save(self, record: RunRecord) -> None: ...

    @abstractmethod
    def list_runs(self) -> tuple[RunRecord, ...]: ...

    # -- artifacts ---------------------------------------------------------------

    @abstractmethod
    def write_artifact(self, run_id: str, filename: str, payload: Any) -> Path: ...

    @abstractmethod
    def read_artifact(self, run_id: str, filename: str) -> Any: ...

    # -- events ------------------------------------------------------------------

    @abstractmethod
    def append_event(self, run_id: str, event: RunEvent) -> None: ...

    @abstractmethod
    def read_events(self, run_id: str) -> tuple[RunEvent, ...]: ...


#: Backoff before retrying the record rename, in seconds.
REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)


def _replace_with_retry(temporary: Path, path: Path) -> None:
    """Rename over the record, tolerating a transient hold on the file.

    On Windows a file that was written a moment ago can still be open in a virus
    scanner or the search indexer, and ``os.replace`` then fails with
    ``PermissionError [WinError 5]`` even though nothing is wrong. A full test run hit
    this once in ``save``; the same failure during a live run would mark the run failed
    while the work it describes was fine, because the runner records a save error as a
    run error. Retrying is the remedy — the holder releases in milliseconds.

    Not conditional on the platform: on POSIX ``rename`` is atomic and never raises
    this, so the retry loop simply never runs, and a genuine permissions problem still
    surfaces after the last attempt rather than being swallowed.
    """
    for delay in REPLACE_RETRY_DELAYS:
        try:
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(delay)
    # Out of retries. A record that cannot be written leaves the run unresumable, so
    # this raises rather than reporting a save that did not happen.
    temporary.replace(path)


class FilesystemRunStore(RunStore):
    """Everything under ``run_root``. The default, and what the tests run against.

    Layout::

        <run_id>/
            run.json            the resumable record
            events.jsonl        append-only audit trail
            artifacts/<node>.json
            logs/               worker stdout, stderr and normalized outcomes
    """

    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root.expanduser().resolve()

    # -- record ------------------------------------------------------------------

    def create(self, record: RunRecord) -> RunRecord:
        run_dir = self.run_dir(record.run_id)
        if run_dir.exists():
            raise RunStoreError(f"run {record.run_id} already exists at {run_dir}")
        self.artifact_dir(record.run_id).mkdir(parents=True)
        self.log_dir(record.run_id).mkdir(parents=True)
        self.save(record)
        return record

    def list_runs(self) -> tuple[RunRecord, ...]:
        """Every readable run, newest first.

        A directory that cannot be parsed is skipped rather than failing the listing:
        one corrupt run must not hide the others.
        """
        if not self.run_root.is_dir():
            return ()
        records: list[RunRecord] = []
        for candidate in self.run_root.iterdir():
            if not (candidate / "run.json").is_file():
                continue
            try:
                records.append(self.load(candidate.name))
            except RunStoreError:
                continue
        records.sort(key=lambda record: record.created_at, reverse=True)
        return tuple(records)

    def load(self, run_id: str) -> RunRecord:
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            raise RunStoreError(f"no such run: {run_id}")
        try:
            return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RunStoreError(f"run {run_id} has an unreadable record: {exc}") from exc

    def save(self, record: RunRecord) -> None:
        path = self.run_dir(record.run_id) / "run.json"
        # Write-then-rename: a crash mid-write must not leave a truncated record that
        # makes the run unresumable.
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        _replace_with_retry(temporary, path)

    # -- artifacts ---------------------------------------------------------------

    def write_artifact(self, run_id: str, filename: str, payload: Any) -> Path:
        path = self.artifact_path(run_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def read_artifact(self, run_id: str, filename: str) -> Any:
        path = self.artifact_path(run_id, filename)
        if not path.is_file():
            raise RunStoreError(f"missing artifact {filename} in run {run_id}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RunStoreError(f"artifact {filename} in run {run_id} is not JSON: {exc}") from exc

    # -- events ------------------------------------------------------------------

    def append_event(self, run_id: str, event: RunEvent) -> None:
        path = self.run_dir(run_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")

    def read_events(self, run_id: str) -> tuple[RunEvent, ...]:
        path = self.run_dir(run_id) / "events.jsonl"
        if not path.is_file():
            return ()
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(RunEvent.model_validate_json(line))
        return tuple(events)
