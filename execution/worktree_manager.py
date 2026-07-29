"""Git worktree lifecycle for write-capable runs.

Implements ADR-005 (a worktree per write task) within the layout ADR-010 requires::

    <workspace_root>/
        RUN-0001/            <- barrier directory: holds the worktree and nothing else
            worktree/        <- the only writable path for the worker

The run directory exists so :class:`~execution.workspace_guard.WriteBarrier` has
something to protect. A worktree placed directly under the workspace root would share
its barrier directory with every other run.

Every git invocation here goes through an argv list; no shell is involved.

The write footprint of these operations on the primary checkout was measured rather
than assumed, and :attr:`Worktree.git_allowances` names exactly the paths involved:

- ``.git/worktrees/<name>/`` — this worktree's HEAD, index, reflog and lock file
- ``.git/refs/heads/<branch>`` and ``.git/logs/refs/heads/<branch>`` — its branch
- ``.git/objects/`` — objects a commit inside the worktree writes

Editing files in the worktree touches none of them. Everything else in the primary
checkout, including its working tree and every other ref, stays protected.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Run identifiers become directory and branch names, so keep them boring.
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

WORKTREE_DIR_NAME = "worktree"


class WorktreeError(RuntimeError):
    """A worktree operation failed, or would have violated a containment invariant."""


@dataclass(frozen=True)
class Worktree:
    """A created worktree and the facts needed to contain and clean it up."""

    run_id: str
    repository: Path
    run_dir: Path
    path: Path
    branch: str
    base_ref: str

    @property
    def git_allowances(self) -> tuple[Path, ...]:
        """Paths in the primary checkout this worktree is expected to write.

        Pass to ``WorkspaceContainment(allowed_paths=...)`` so a worker committing on
        its own branch is not reported as an escape, while any other write to the
        primary checkout still is.
        """
        git_dir = self.repository / ".git"
        return (
            git_dir / "worktrees" / self.path.name,
            git_dir / "refs" / "heads" / Path(self.branch),
            git_dir / "logs" / "refs" / "heads" / Path(self.branch),
            git_dir / "objects",
        )


@dataclass(frozen=True)
class WorktreeRecord:
    """One entry from ``git worktree list --porcelain``."""

    path: Path
    head: str | None
    branch: str | None
    locked: bool
    prunable: str | None


@dataclass(frozen=True)
class ReconciliationReport:
    """What a restart found. Reporting only — nothing destructive happens implicitly."""

    pruned: tuple[Path, ...]
    locked: tuple[Path, ...]
    orphaned_run_dirs: tuple[Path, ...]

    @property
    def clean(self) -> bool:
        return not (self.pruned or self.locked or self.orphaned_run_dirs)


class WorktreeManager:
    """Creates, validates, locks, cleans up and reconciles run worktrees."""

    def __init__(self, repository: Path, workspace_root: Path) -> None:
        self.repository = repository.resolve()
        self.workspace_root = workspace_root.expanduser().resolve()

        if (
            self.workspace_root == self.repository
            or self.repository in self.workspace_root.parents
        ):
            raise WorktreeError(
                f"workspace_root {self.workspace_root} is inside the repository "
                f"{self.repository}; a worker escape would reach the primary checkout"
            )
        if not (self.repository / ".git").exists():
            raise WorktreeError(f"not a git repository: {self.repository}")

    # -- lifecycle ---------------------------------------------------------------

    def create(self, run_id: str, *, base_ref: str = "HEAD", branch: str | None = None) -> Worktree:
        """Create ``<workspace_root>/<run_id>/worktree`` on a fresh branch."""
        self._validate_run_id(run_id)
        branch = branch or f"run/{run_id}"

        run_dir = self.workspace_root / run_id
        worktree_path = run_dir / WORKTREE_DIR_NAME

        if worktree_path.exists():
            raise WorktreeError(f"worktree already exists: {worktree_path}")
        if run_dir.exists() and any(run_dir.iterdir()):
            raise WorktreeError(
                f"run directory {run_dir} is not empty; the write barrier can only "
                "protect a directory that holds the worktree alone"
            )
        if self._branch_exists(branch):
            raise WorktreeError(
                f"branch {branch} already exists; reusing it would mix two runs' history"
            )

        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._git("worktree", "add", "-b", branch, str(worktree_path), base_ref)
        except WorktreeError:
            # Leave nothing half-built for the next run to trip over.
            if run_dir.exists() and not any(run_dir.iterdir()):
                run_dir.rmdir()
            raise

        return Worktree(
            run_id=run_id,
            repository=self.repository,
            run_dir=run_dir,
            path=worktree_path,
            branch=branch,
            base_ref=base_ref,
        )

    def validate(self, worktree: Worktree) -> None:
        """Raise unless the worktree is still in the shape containment assumes."""
        if not worktree.path.is_dir():
            raise WorktreeError(f"worktree directory is missing: {worktree.path}")
        if self.workspace_root not in worktree.path.parents:
            raise WorktreeError(f"worktree {worktree.path} is outside {self.workspace_root}")

        siblings = [entry for entry in worktree.run_dir.iterdir() if entry != worktree.path]
        if siblings:
            raise WorktreeError(
                f"run directory {worktree.run_dir} gained entries beside the worktree: "
                f"{[entry.name for entry in siblings]}"
            )

        record = self._find_record(worktree.path)
        if record is None:
            raise WorktreeError(f"git does not know about the worktree at {worktree.path}")
        if record.prunable is not None:
            raise WorktreeError(f"git considers the worktree stale: {record.prunable}")
        if record.branch is not None and record.branch != worktree.branch:
            raise WorktreeError(
                f"worktree {worktree.path} is on {record.branch}, expected {worktree.branch}"
            )

    def lock(self, worktree: Worktree, reason: str) -> None:
        """Lock the worktree so cleanup cannot remove it out from under a live run."""
        self._git("worktree", "lock", "--reason", reason, str(worktree.path))

    def unlock(self, worktree: Worktree) -> None:
        self._git("worktree", "unlock", str(worktree.path))

    def cleanup(self, worktree: Worktree, *, force: bool = False) -> None:
        """Remove the worktree and its run directory.

        A locked worktree is refused unless ``force`` is set: the lock is how a live
        run says it is still using the directory. ``git worktree remove`` leaves the
        run directory behind, so it is removed here too, and only ever from inside
        ``workspace_root``.
        """
        record = self._find_record(worktree.path)
        if record is not None and record.locked and not force:
            raise WorktreeError(f"worktree {worktree.path} is locked; unlock it or pass force=True")

        if record is not None:
            arguments = ["worktree", "remove", str(worktree.path)]
            if force:
                # Two -f: one for the lock, one for uncommitted changes.
                arguments[2:2] = ["-f", "-f"]
            self._git(*arguments)

        self._remove_run_dir(worktree.run_dir)

    def reconcile(self) -> ReconciliationReport:
        """Restart housekeeping: prune worktrees git can prove are gone, report the rest.

        Deliberately non-destructive beyond ``git worktree prune``. A locked worktree may
        belong to a run that outlived this process, and an unregistered run directory may
        hold the only copy of a worker's output. Both are reported for a higher layer to
        decide on.
        """
        prunable = tuple(
            record.path for record in self.list_worktrees() if record.prunable is not None
        )
        if prunable:
            self._git("worktree", "prune")

        after = self.list_worktrees()
        locked = tuple(record.path for record in after if record.locked)
        registered = {record.path for record in after}

        orphaned: list[Path] = []
        if self.workspace_root.is_dir():
            for run_dir in sorted(self.workspace_root.iterdir()):
                if not run_dir.is_dir():
                    continue
                if (run_dir / WORKTREE_DIR_NAME).resolve() not in registered:
                    orphaned.append(run_dir)

        return ReconciliationReport(
            pruned=prunable,
            locked=locked,
            orphaned_run_dirs=tuple(orphaned),
        )

    # -- inspection --------------------------------------------------------------

    def list_worktrees(self) -> tuple[WorktreeRecord, ...]:
        output = self._git("worktree", "list", "--porcelain")
        records: list[WorktreeRecord] = []
        path: Path | None = None
        head: str | None = None
        branch: str | None = None
        locked = False
        prunable: str | None = None

        def flush() -> None:
            nonlocal path, head, branch, locked, prunable
            if path is not None:
                records.append(WorktreeRecord(path, head, branch, locked, prunable))
            path, head, branch, locked, prunable = None, None, None, False, None

        for line in output.splitlines():
            if not line.strip():
                flush()
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                flush()
                path = Path(value).resolve()
            elif key == "HEAD":
                head = value
            elif key == "branch":
                branch = value.removeprefix("refs/heads/")
            elif key == "locked":
                locked = True
            elif key == "prunable":
                prunable = value or "unspecified"
        flush()
        return tuple(records)

    # -- internals ---------------------------------------------------------------

    def _find_record(self, path: Path) -> WorktreeRecord | None:
        resolved = path.resolve()
        for record in self.list_worktrees():
            if record.path == resolved:
                return record
        return None

    def _branch_exists(self, branch: str) -> bool:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0

    def _remove_run_dir(self, run_dir: Path) -> None:
        resolved = run_dir.resolve()
        if self.workspace_root not in resolved.parents:
            raise WorktreeError(
                f"refusing to delete {resolved}: outside workspace root {self.workspace_root}"
            )
        if resolved.exists():
            shutil.rmtree(resolved)

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise WorktreeError(
                f"git {' '.join(arguments)} failed with exit {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        return completed.stdout

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not RUN_ID_PATTERN.match(run_id):
            raise WorktreeError(
                f"invalid run id {run_id!r}: expected 1-64 chars of [A-Za-z0-9._-] "
                "starting alphanumeric"
            )
