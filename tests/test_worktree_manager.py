"""Tests for the worktree lifecycle.

These drive a real `git`. The module's whole job is to be correct about git's actual
behaviour — the M1 spike's lesson was that documented behaviour and observed behaviour
diverge — so mocking the subprocess would test the mock.
"""

import subprocess
from pathlib import Path

import pytest

from execution.worktree_manager import (
    WORKTREE_DIR_NAME,
    WorktreeError,
    WorktreeManager,
)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@l", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A primary checkout with one commit on `main`."""
    repo = tmp_path / "primary"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    return repo


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture
def manager(repository: Path, workspace_root: Path) -> WorktreeManager:
    return WorktreeManager(repository, workspace_root)


# ---------------------------------------------------------------------------
# Construction invariants
# ---------------------------------------------------------------------------


def test_workspace_root_inside_the_repository_is_refused(repository: Path) -> None:
    with pytest.raises(WorktreeError, match="inside the repository"):
        WorktreeManager(repository, repository / "worktrees")


def test_a_non_repository_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not a git repository"):
        WorktreeManager(plain, tmp_path / "workspaces")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_builds_the_adr_010_layout(manager: WorktreeManager, workspace_root: Path) -> None:
    worktree = manager.create("RUN-0001")

    assert worktree.path == workspace_root / "RUN-0001" / WORKTREE_DIR_NAME
    assert worktree.path.is_dir()
    assert worktree.branch == "run/RUN-0001"
    assert (worktree.path / "app.py").read_text().strip() == "print('hello')"
    # The barrier can only protect a run directory holding the worktree alone.
    assert [entry.name for entry in worktree.run_dir.iterdir()] == [WORKTREE_DIR_NAME]


def test_create_does_not_touch_the_primary_working_tree(
    manager: WorktreeManager, repository: Path
) -> None:
    before = (repository / "app.py").read_text()
    manager.create("RUN-0001")
    assert (repository / "app.py").read_text() == before
    assert not (repository / WORKTREE_DIR_NAME).exists()


def test_create_rejects_a_duplicate_run(manager: WorktreeManager) -> None:
    manager.create("RUN-0001")
    with pytest.raises(WorktreeError, match="already exists"):
        manager.create("RUN-0001")


def test_create_rejects_a_non_empty_run_directory(
    manager: WorktreeManager, workspace_root: Path
) -> None:
    run_dir = workspace_root / "RUN-0001"
    run_dir.mkdir()
    (run_dir / "stray.txt").write_text("x", encoding="utf-8")

    with pytest.raises(WorktreeError, match="is not empty"):
        manager.create("RUN-0001")


def test_create_rejects_an_existing_branch(manager: WorktreeManager, repository: Path) -> None:
    _git("branch", "run/RUN-0001", cwd=repository)
    with pytest.raises(WorktreeError, match="already exists"):
        manager.create("RUN-0001")


def test_create_leaves_no_debris_when_git_fails(
    manager: WorktreeManager, workspace_root: Path
) -> None:
    with pytest.raises(WorktreeError, match="git worktree add"):
        manager.create("RUN-0001", base_ref="no-such-ref")
    assert not (workspace_root / "RUN-0001").exists()


@pytest.mark.parametrize("run_id", ["", "..", "../escape", "with space", "a" * 65, "/abs"])
def test_create_rejects_unsafe_run_ids(manager: WorktreeManager, run_id: str) -> None:
    with pytest.raises(WorktreeError, match="invalid run id"):
        manager.create(run_id)


# ---------------------------------------------------------------------------
# git_allowances
# ---------------------------------------------------------------------------


def test_allowances_cover_a_commit_made_inside_the_worktree(manager: WorktreeManager) -> None:
    """A worker committing on its own branch must not register as an escape."""
    from execution.workspace_guard import EscapeDetector

    worktree = manager.create("RUN-0001")
    detector = EscapeDetector([manager.repository], allowed_paths=worktree.git_allowances)
    before = detector.snapshot()

    (worktree.path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    _git("add", "-A", cwd=worktree.path)
    _git("commit", "-q", "-m", "worker change", cwd=worktree.path)

    assert detector.compare(before, detector.snapshot()) == ()


def test_allowances_do_not_excuse_a_write_to_the_primary_working_tree(
    manager: WorktreeManager, repository: Path
) -> None:
    from execution.workspace_guard import EscapeDetector

    worktree = manager.create("RUN-0001")
    detector = EscapeDetector([repository], allowed_paths=worktree.git_allowances)
    before = detector.snapshot()

    (repository / "PLANTED.txt").write_text("ESCAPED", encoding="utf-8")

    violations = detector.compare(before, detector.snapshot())
    assert [(v.kind, v.path.name) for v in violations] == [("created", "PLANTED.txt")]


def test_allowances_do_not_excuse_another_branch(
    manager: WorktreeManager, repository: Path
) -> None:
    from execution.workspace_guard import EscapeDetector

    worktree = manager.create("RUN-0001")
    detector = EscapeDetector([repository], allowed_paths=worktree.git_allowances)
    before = detector.snapshot()

    _git("branch", "someone-elses-branch", cwd=repository)

    violations = detector.compare(before, detector.snapshot())
    assert any("someone-elses-branch" in str(v.path) for v in violations)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_accepts_a_healthy_worktree(manager: WorktreeManager) -> None:
    manager.validate(manager.create("RUN-0001"))


def test_validate_rejects_a_deleted_worktree(manager: WorktreeManager) -> None:
    import shutil

    worktree = manager.create("RUN-0001")
    shutil.rmtree(worktree.path)

    with pytest.raises(WorktreeError, match="directory is missing"):
        manager.validate(worktree)


def test_validate_rejects_a_polluted_run_directory(manager: WorktreeManager) -> None:
    worktree = manager.create("RUN-0001")
    (worktree.run_dir / "appeared.txt").write_text("x", encoding="utf-8")

    with pytest.raises(WorktreeError, match="gained entries beside the worktree"):
        manager.validate(worktree)


def test_validate_rejects_a_switched_branch(manager: WorktreeManager) -> None:
    worktree = manager.create("RUN-0001")
    _git("checkout", "-q", "-b", "hijacked", cwd=worktree.path)

    with pytest.raises(WorktreeError, match="expected run/RUN-0001"):
        manager.validate(worktree)


# ---------------------------------------------------------------------------
# lock / cleanup
# ---------------------------------------------------------------------------


def test_lock_blocks_cleanup(manager: WorktreeManager) -> None:
    worktree = manager.create("RUN-0001")
    manager.lock(worktree, "run in flight")

    with pytest.raises(WorktreeError, match="is locked"):
        manager.cleanup(worktree)
    assert worktree.path.is_dir()


def test_unlock_then_cleanup_removes_everything(manager: WorktreeManager) -> None:
    worktree = manager.create("RUN-0001")
    manager.lock(worktree, "run in flight")
    manager.unlock(worktree)
    manager.cleanup(worktree)

    assert not worktree.path.exists()
    # git worktree remove leaves the run directory behind; cleanup must not.
    assert not worktree.run_dir.exists()
    assert manager._find_record(worktree.path) is None


def test_force_cleanup_overrides_a_lock_and_uncommitted_changes(manager: WorktreeManager) -> None:
    worktree = manager.create("RUN-0001")
    (worktree.path / "app.py").write_text("uncommitted\n", encoding="utf-8")
    manager.lock(worktree, "run in flight")

    manager.cleanup(worktree, force=True)
    assert not worktree.run_dir.exists()


def test_cleanup_refuses_a_run_dir_outside_the_workspace(
    manager: WorktreeManager, tmp_path: Path
) -> None:
    from dataclasses import replace

    worktree = manager.create("RUN-0001")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    forged = replace(worktree, run_dir=elsewhere)

    manager.cleanup(worktree)  # tidy the real one first
    with pytest.raises(WorktreeError, match="outside workspace root"):
        manager._remove_run_dir(forged.run_dir)
    assert elsewhere.exists()


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


def test_reconcile_is_clean_when_nothing_is_outstanding(manager: WorktreeManager) -> None:
    assert manager.reconcile().clean is True


def test_reconcile_prunes_a_worktree_deleted_behind_gits_back(manager: WorktreeManager) -> None:
    import shutil

    worktree = manager.create("RUN-0001")
    shutil.rmtree(worktree.path)

    report = manager.reconcile()

    assert worktree.path in report.pruned
    assert manager._find_record(worktree.path) is None
    # The empty run directory is left for a higher layer to decide about.
    assert report.orphaned_run_dirs == (worktree.run_dir,)


def test_reconcile_reports_a_locked_worktree_without_touching_it(
    manager: WorktreeManager,
) -> None:
    worktree = manager.create("RUN-0001")
    manager.lock(worktree, "survived a restart")

    report = manager.reconcile()

    assert report.locked == (worktree.path,)
    assert report.clean is False
    assert worktree.path.is_dir()


def test_reconcile_reports_an_unregistered_run_directory(
    manager: WorktreeManager, workspace_root: Path
) -> None:
    orphan = workspace_root / "RUN-ORPHAN"
    (orphan / WORKTREE_DIR_NAME).mkdir(parents=True)
    (orphan / WORKTREE_DIR_NAME / "output.txt").write_text("work", encoding="utf-8")

    report = manager.reconcile()

    assert report.orphaned_run_dirs == (orphan,)
    # Reporting only: the worker's only copy of its output must survive.
    assert (orphan / WORKTREE_DIR_NAME / "output.txt").exists()
