"""Tests for workspace containment (ADR-010, spike blocker B2).

The barrier tests exercise the real OS mechanism rather than a mock: the whole point
of B2 is that a stated sandbox guarantee turned out not to hold, so a test asserting
that we called `icacls` would prove nothing. They write to the filesystem and check
whether the write succeeded.
"""

import os
from pathlib import Path

import pytest

from execution.workspace_guard import (
    ContainmentError,
    EscapeDetector,
    WorkspaceContainment,
    WriteBarrier,
)


def _make_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Build `run_dir/worktree`, the layout WorkspaceContainment requires."""
    run_dir = tmp_path / "RUN-0001"
    worktree = run_dir / "worktree"
    worktree.mkdir(parents=True)
    return run_dir, worktree


# ---------------------------------------------------------------------------
# WriteBarrier
# ---------------------------------------------------------------------------


def test_barrier_blocks_creation_in_the_barrier_directory(tmp_path: Path) -> None:
    run_dir, _ = _make_layout(tmp_path)

    with WriteBarrier(run_dir) as barrier:
        assert barrier.mechanism in {"icacls_deny", "chmod_ro"}
        with pytest.raises(OSError):
            (run_dir / "escape.txt").write_text("ESCAPED", encoding="utf-8")

    assert not (run_dir / "escape.txt").exists()


def test_barrier_leaves_the_worktree_writable(tmp_path: Path) -> None:
    """A barrier that also blocks legitimate work is useless."""
    run_dir, worktree = _make_layout(tmp_path)

    with WriteBarrier(run_dir):
        (worktree / "legitimate.txt").write_text("work", encoding="utf-8")
        (worktree / "nested").mkdir()
        (worktree / "nested" / "deep.txt").write_text("work", encoding="utf-8")

    assert (worktree / "legitimate.txt").read_text() == "work"
    assert (worktree / "nested" / "deep.txt").read_text() == "work"


def test_barrier_release_restores_writability(tmp_path: Path) -> None:
    run_dir, _ = _make_layout(tmp_path)

    barrier = WriteBarrier(run_dir)
    barrier.apply()
    barrier.release()

    (run_dir / "after.txt").write_text("ok", encoding="utf-8")
    assert (run_dir / "after.txt").exists()
    assert barrier.mechanism == "not_applied"


def test_barrier_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ContainmentError, match="does not exist"):
        WriteBarrier(tmp_path / "nope").apply()


# ---------------------------------------------------------------------------
# EscapeDetector
# ---------------------------------------------------------------------------


def test_detector_reports_nothing_when_protected_paths_are_untouched(tmp_path: Path) -> None:
    protected = tmp_path / "checkout"
    protected.mkdir()
    (protected / "source.py").write_text("print(1)", encoding="utf-8")

    detector = EscapeDetector([protected])
    before = detector.snapshot()
    after = detector.snapshot()

    assert detector.compare(before, after) == ()


def test_detector_reports_created_modified_and_deleted(tmp_path: Path) -> None:
    protected = tmp_path / "checkout"
    protected.mkdir()
    (protected / "kept.py").write_text("stable", encoding="utf-8")
    (protected / "changed.py").write_text("before", encoding="utf-8")
    (protected / "removed.py").write_text("bye", encoding="utf-8")

    detector = EscapeDetector([protected])
    before = detector.snapshot()

    (protected / "added.py").write_text("new", encoding="utf-8")
    (protected / "changed.py").write_text("after and longer", encoding="utf-8")
    (protected / "removed.py").unlink()

    violations = detector.compare(before, detector.snapshot())
    by_kind = {violation.kind: violation.path.name for violation in violations}

    assert by_kind == {
        "created": "added.py",
        "modified": "changed.py",
        "deleted": "removed.py",
    }


def test_detector_skips_noisy_directories(tmp_path: Path) -> None:
    protected = tmp_path / "checkout"
    (protected / "node_modules" / "pkg").mkdir(parents=True)
    detector = EscapeDetector([protected])
    before = detector.snapshot()

    (protected / "node_modules" / "pkg" / "index.js").write_text("noise", encoding="utf-8")

    assert detector.compare(before, detector.snapshot()) == ()


def test_detector_refuses_an_oversized_tree(tmp_path: Path) -> None:
    protected = tmp_path / "checkout"
    protected.mkdir()
    for index in range(5):
        (protected / f"file{index}.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ContainmentError, match="exceeds 2 files"):
        EscapeDetector([protected], max_files=2).snapshot()


# ---------------------------------------------------------------------------
# WorkspaceContainment
# ---------------------------------------------------------------------------


def test_containment_reports_a_clean_run(tmp_path: Path) -> None:
    run_dir, worktree = _make_layout(tmp_path)
    protected = tmp_path / "checkout"
    protected.mkdir()
    (protected / "source.py").write_text("print(1)", encoding="utf-8")

    containment = WorkspaceContainment(worktree, protected_roots=[protected])
    containment.arm()
    (worktree / "output.txt").write_text("legitimate work", encoding="utf-8")
    report = containment.disarm()

    assert report.contained is True
    assert report.violations == ()
    assert report.files_scanned == 1


def test_containment_catches_an_absolute_path_write(tmp_path: Path) -> None:
    """The case the barrier cannot prevent — this is why detection exists."""
    run_dir, worktree = _make_layout(tmp_path)
    protected = tmp_path / "checkout"
    protected.mkdir()
    (protected / "source.py").write_text("print(1)", encoding="utf-8")

    containment = WorkspaceContainment(worktree, protected_roots=[protected])
    containment.arm()
    # Nothing stops this: it does not go through the barrier directory at all.
    (protected / "planted.py").write_text("import evil", encoding="utf-8")
    report = containment.disarm()

    assert report.contained is False
    assert [(v.kind, v.path.name) for v in report.violations] == [("created", "planted.py")]


def test_containment_rejects_a_worktree_inside_a_protected_root(tmp_path: Path) -> None:
    protected = tmp_path / "checkout"
    worktree = protected / "worktrees" / "RUN-0001"
    worktree.mkdir(parents=True)

    containment = WorkspaceContainment(worktree, protected_roots=[protected])
    with pytest.raises(ContainmentError, match="sits inside protected root"):
        containment.arm()


def test_containment_rejects_a_shared_barrier_directory(tmp_path: Path) -> None:
    """The barrier cannot protect files that already sit beside the worktree."""
    run_dir, worktree = _make_layout(tmp_path)
    (run_dir / "stray.txt").write_text("pre-existing", encoding="utf-8")

    containment = WorkspaceContainment(worktree, protected_roots=[tmp_path / "checkout"])
    with pytest.raises(ContainmentError, match="must contain only the worktree"):
        containment.arm()


def test_containment_requires_arm_before_disarm(tmp_path: Path) -> None:
    _, worktree = _make_layout(tmp_path)
    containment = WorkspaceContainment(worktree, protected_roots=[])

    with pytest.raises(ContainmentError, match="disarm\\(\\) called before arm\\(\\)"):
        containment.disarm()


def test_containment_releases_the_barrier_even_after_a_violation(tmp_path: Path) -> None:
    run_dir, worktree = _make_layout(tmp_path)
    protected = tmp_path / "checkout"
    protected.mkdir()

    containment = WorkspaceContainment(worktree, protected_roots=[protected])
    containment.arm()
    (protected / "planted.py").write_text("x", encoding="utf-8")
    report = containment.disarm()

    assert report.contained is False
    # Cleanup must still be possible, otherwise the run directory is unremovable.
    (run_dir / "cleanup-probe.txt").write_text("ok", encoding="utf-8")
    assert os.access(run_dir, os.W_OK)
