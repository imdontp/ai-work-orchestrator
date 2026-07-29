"""One contract, both storage backends.

Every test here runs against FilesystemRunStore and PostgresRunStore, so the two
cannot drift into behaving differently. The Postgres parametrization skips when no
database is reachable — `make postgres-up` provides one.
"""

import json
import os
import time
from pathlib import Path

import pytest

from orchestrator.domain.models import TaskState
from orchestrator.workflow.store import (
    REPLACE_RETRY_DELAYS,
    ApprovalRequest,
    FilesystemRunStore,
    RunEvent,
    RunRecord,
    RunStoreError,
)

DEFAULT_DSN = "postgresql://orchestrator:orchestrator@127.0.0.1:5432/orchestrator"
DSN = os.environ.get("AIWO_TEST_DATABASE_URL", DEFAULT_DSN)

#: These tests TRUNCATE. Pointing them at a database that is not plainly a local
#: throwaway would destroy it, so refuse rather than trust the operator got it right.
_LOCAL_HOSTS = ("@127.0.0.1", "@localhost", "@postgres:")


def _is_local_throwaway(dsn: str) -> bool:
    return any(host in dsn for host in _LOCAL_HOSTS)


if not _is_local_throwaway(DSN):
    raise RuntimeError(
        f"refusing to run destructive store tests against {DSN}: "
        "these tests truncate every table, so the database must be local"
    )


def _test_dsn(dsn: str) -> str:
    """Point at a `_test` sibling of the configured database.

    Truncating the configured database was not hypothetical: running this suite while a
    live run was in flight deleted it mid-flight, because both shared `orchestrator`.
    A separate database means the suite cannot reach anything anyone else is using,
    whatever the host check says.
    """
    base, separator, name = dsn.rpartition("/")
    if not separator or not name:
        raise RuntimeError(f"cannot derive a test database from {dsn}")
    return f"{base}/{name}_test"


TEST_DSN = _test_dsn(DSN)


def _postgres_available() -> bool:
    """True when a server is reachable and the test database exists or can be made."""
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is a declared dependency
        return False
    try:
        with psycopg.connect(TEST_DSN, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001 - probably just missing; try to create it
        pass
    try:
        name = TEST_DSN.rpartition("/")[2]
        with psycopg.connect(DSN, connect_timeout=3, autocommit=True) as connection:
            connection.execute(f'CREATE DATABASE "{name}"')
        return True
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return False


postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason=f"no PostgreSQL at {TEST_DSN}; run `make postgres-up`",
)


def _record(run_id: str = "RUN-1", **overrides) -> RunRecord:
    payload = {
        "run_id": run_id,
        "task_id": "TASK-1",
        "task": {"task_id": "TASK-1", "objective": "do the thing"},
        "workflow_id": "wf",
        "workflow_version": "0.1.0",
    }
    payload.update(overrides)
    return RunRecord(**payload)


@pytest.fixture(params=["filesystem", "postgres"])
def store(request, tmp_path: Path):
    if request.param == "filesystem":
        return FilesystemRunStore(tmp_path / "runs")

    if not _postgres_available():
        pytest.skip(f"no PostgreSQL at {TEST_DSN}; run `make postgres-up`")

    import psycopg

    from orchestrator.workflow.postgres_store import PostgresRunStore

    backend = PostgresRunStore(TEST_DSN, tmp_path / "runs")
    # Each test gets a clean database. run_events and run_artifacts cascade.
    with psycopg.connect(TEST_DSN, autocommit=True) as connection:
        connection.execute("TRUNCATE runs CASCADE")
    return backend


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def test_a_created_run_can_be_loaded_back(store) -> None:
    store.create(_record())

    loaded = store.load("RUN-1")

    assert loaded.run_id == "RUN-1"
    assert loaded.task_id == "TASK-1"
    assert loaded.task["objective"] == "do the thing"
    assert loaded.task_state is TaskState.PENDING


def test_creating_the_same_run_twice_is_refused(store) -> None:
    store.create(_record())

    with pytest.raises(RunStoreError, match="already exists"):
        store.create(_record())


def test_loading_an_unknown_run_is_refused(store) -> None:
    with pytest.raises(RunStoreError, match="no such run"):
        store.load("RUN-nope")


def test_saving_replaces_the_record(store) -> None:
    record = store.create(_record())
    record.task_state = TaskState.RUNNING
    record.completed_nodes.append("analyze")
    record.repair_rounds = 2
    store.save(record)

    loaded = store.load("RUN-1")

    assert loaded.task_state is TaskState.RUNNING
    assert loaded.completed_nodes == ["analyze"]
    assert loaded.repair_rounds == 2


def test_a_pending_approval_round_trips(store) -> None:
    """Nested models must survive storage, or a paused run cannot be resumed."""
    record = store.create(_record())
    record.pending_approval = ApprovalRequest(
        approval_id="APPROVAL-1",
        run_id="RUN-1",
        approval_type="plan",
        risk_level="medium",
        summary="approve the plan",
        evidence=["a.json"],
        resume_after_node="analyze",
    )
    store.save(record)

    loaded = store.load("RUN-1")

    assert loaded.pending_approval is not None
    assert loaded.pending_approval.approval_id == "APPROVAL-1"
    assert loaded.pending_approval.resume_after_node == "analyze"


def test_the_worktree_reference_round_trips(store) -> None:
    record = store.create(_record())
    record.worktree = {"path": "C:/w", "branch": "run/RUN-1", "base_ref": "HEAD",
                       "run_id": "RUN-1", "repository": "C:/r", "run_dir": "C:/d"}
    store.save(record)

    assert store.load("RUN-1").worktree["branch"] == "run/RUN-1"


def test_listing_returns_the_newest_first(store) -> None:
    store.create(_record("RUN-old", created_at="2026-01-01T00:00:00+00:00"))
    store.create(_record("RUN-new", created_at="2026-07-01T00:00:00+00:00"))

    assert [r.run_id for r in store.list_runs()] == ["RUN-new", "RUN-old"]


def test_listing_an_empty_store_is_empty(store) -> None:
    assert store.list_runs() == ()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_events_read_back_in_the_order_they_were_appended(store) -> None:
    store.create(_record())
    for index in range(12):
        store.append_event("RUN-1", RunEvent(kind=f"step-{index}", node_id="n"))

    kinds = [event.kind for event in store.read_events("RUN-1")]

    assert kinds == [f"step-{index}" for index in range(12)]


def test_event_detail_survives_storage(store) -> None:
    store.create(_record())
    store.append_event(
        "RUN-1",
        RunEvent(kind="verification_finished", detail={"passed": True, "claimed_passed": None}),
    )

    event = store.read_events("RUN-1")[0]

    assert event.detail == {"passed": True, "claimed_passed": None}
    assert event.kind == "verification_finished"


def test_a_run_with_no_events_reads_empty(store) -> None:
    store.create(_record())
    assert store.read_events("RUN-1") == ()


def test_events_are_not_rewritten_by_a_save(store) -> None:
    """The audit trail is append-only; updating the record must not disturb it."""
    record = store.create(_record())
    store.append_event("RUN-1", RunEvent(kind="run_created"))
    record.task_state = TaskState.RUNNING
    store.save(record)
    store.append_event("RUN-1", RunEvent(kind="node_started"))

    assert [e.kind for e in store.read_events("RUN-1")] == ["run_created", "node_started"]


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def test_an_artifact_round_trips(store) -> None:
    store.create(_record())

    path = store.write_artifact("RUN-1", "analysis.json", {"verdict": "pass", "n": 1})

    assert path.is_file()
    assert store.read_artifact("RUN-1", "analysis.json") == {"verdict": "pass", "n": 1}


def test_artifacts_are_files_on_disk_whatever_the_backend(store) -> None:
    """A worker writes files; storage choice does not change that."""
    store.create(_record())
    store.write_artifact("RUN-1", "analysis.json", {"a": 1})

    assert store.artifact_path("RUN-1", "analysis.json").is_file()
    assert json.loads(store.artifact_path("RUN-1", "analysis.json").read_text()) == {"a": 1}


def test_rewriting_an_artifact_replaces_it(store) -> None:
    store.create(_record())
    store.write_artifact("RUN-1", "a.json", {"round": 1})
    store.write_artifact("RUN-1", "a.json", {"round": 2})

    assert store.read_artifact("RUN-1", "a.json") == {"round": 2}


def test_reading_a_missing_artifact_is_refused(store) -> None:
    store.create(_record())

    with pytest.raises(RunStoreError, match="missing artifact"):
        store.read_artifact("RUN-1", "nope.json")


def test_the_log_directory_exists_after_create(store) -> None:
    store.create(_record())
    assert store.log_dir("RUN-1").is_dir()


# ---------------------------------------------------------------------------
# Postgres-specific guarantees
# ---------------------------------------------------------------------------


@postgres
def test_postgres_detects_an_artifact_changed_behind_its_back(tmp_path: Path) -> None:
    """The metadata records a hash; content that no longer matches is not served."""
    import psycopg

    from orchestrator.workflow.postgres_store import PostgresRunStore

    with psycopg.connect(TEST_DSN, autocommit=True) as connection:
        connection.execute("TRUNCATE runs CASCADE")
    store = PostgresRunStore(TEST_DSN, tmp_path / "runs")
    store.create(_record())
    path = store.write_artifact("RUN-1", "a.json", {"trusted": True})

    path.write_text(json.dumps({"trusted": False}), encoding="utf-8")

    with pytest.raises(RunStoreError, match="does not match its recorded sha256"):
        store.read_artifact("RUN-1", "a.json")


@postgres
def test_postgres_records_artifact_metadata(tmp_path: Path) -> None:
    from orchestrator.workflow.postgres_store import PostgresRunStore

    store = PostgresRunStore(TEST_DSN, tmp_path / "runs")
    store.create(_record("RUN-meta"))
    store.write_artifact("RUN-meta", "a.json", {"a": 1})

    assert store.artifact_digest("RUN-meta", "a.json") is not None
    assert store.artifact_digest("RUN-meta", "missing.json") is None


@postgres
def test_a_second_store_instance_sees_the_same_runs(tmp_path: Path) -> None:
    """The point of a database: a different process reads the same state."""
    from orchestrator.workflow.postgres_store import PostgresRunStore

    first = PostgresRunStore(TEST_DSN, tmp_path / "runs")
    first.create(_record("RUN-shared"))
    first.append_event("RUN-shared", RunEvent(kind="run_created"))

    second = PostgresRunStore(TEST_DSN, tmp_path / "runs")

    assert second.load("RUN-shared").task_id == "TASK-1"
    assert [e.kind for e in second.read_events("RUN-shared")] == ["run_created"]


# ---------------------------------------------------------------------------
# Filesystem record durability
# ---------------------------------------------------------------------------


def test_a_transiently_locked_record_is_retried_rather_than_failing_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    """A scanner holding run.json for a moment must not fail the run it describes."""
    store = FilesystemRunStore(tmp_path / "runs")
    record = store.create(_record())
    real_replace = Path.replace
    attempts = {"count": 0}

    def flaky_replace(self: Path, target):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    record.task_state = TaskState.RUNNING
    store.save(record)

    assert attempts["count"] == 3
    assert store.load("RUN-1").task_state is TaskState.RUNNING


def test_a_record_that_stays_locked_still_fails(tmp_path: Path, monkeypatch) -> None:
    """Retrying forever would report a save that never happened."""
    store = FilesystemRunStore(tmp_path / "runs")
    record = store.create(_record())

    def always_denied(self: Path, target):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "replace", always_denied)

    with pytest.raises(PermissionError):
        store.save(record)


def test_the_retry_does_not_slow_the_ordinary_save(tmp_path: Path) -> None:
    """The happy path must not pay for the retry: first attempt, no sleep."""
    store = FilesystemRunStore(tmp_path / "runs")
    record = store.create(_record())

    started = time.monotonic()
    store.save(record)

    assert time.monotonic() - started < min(REPLACE_RETRY_DELAYS)


def test_the_suite_refuses_a_database_that_is_not_local() -> None:
    """These tests truncate. A remote DSN must stop the suite, not be trusted."""
    assert _is_local_throwaway(DEFAULT_DSN)
    assert not _is_local_throwaway("postgresql://u:p@prod.internal:5432/orchestrator")


def test_a_sqlalchemy_style_url_is_normalized() -> None:
    """.env ships `postgresql+psycopg://`, which psycopg itself rejects."""
    from orchestrator.workflow.postgres_store import normalize_dsn

    assert normalize_dsn("postgresql+psycopg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert normalize_dsn("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"

    with pytest.raises(RunStoreError, match="not a database URL"):
        normalize_dsn("orchestrator")
