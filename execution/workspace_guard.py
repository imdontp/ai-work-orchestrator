"""Workspace containment for write-capable worker runs.

Implements ADR-010. The M1 capability spike observed a worker writing above its own
workspace root while running under that worker's own sandbox flag, so containment is
provided here instead of being delegated to the CLI.

Two mechanisms, because they fail differently:

``WriteBarrier``
    An OS-level deny-write rule on the directory that *contains* the worktree. This
    stops the relative ``../escape.txt`` write that was actually observed, at the
    filesystem, before it happens.

``EscapeDetector``
    A before/after fingerprint of the protected paths. This catches everything the
    barrier cannot — most importantly a write to an unrelated absolute path, which
    no mechanism on this platform was shown to prevent.

``WorkspaceContainment`` composes the two around a single run.

Provider-agnostic: nothing here knows which CLI is being contained.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

#: Directories skipped when fingerprinting a protected root. These churn for reasons
#: unrelated to worker behaviour and would drown real violations in noise. ``.git`` is
#: deliberately absent: corrupting the primary checkout's history is exactly the
#: outcome this guard exists to catch.
DEFAULT_EXCLUDED_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".turbo",
        "dist",
        "build",
        ".egg-info",
    }
)

#: Refuse to fingerprint an unexpectedly huge tree rather than stalling a run.
DEFAULT_MAX_FILES = 200_000


class ContainmentError(RuntimeError):
    """The workspace layout cannot be contained, so the run must not start."""


@dataclass(frozen=True)
class EscapeViolation:
    path: Path
    kind: str  # "created" | "modified" | "deleted"

    def __str__(self) -> str:
        return f"{self.kind}: {self.path}"


@dataclass(frozen=True)
class ContainmentReport:
    """Outcome of one contained run."""

    barrier: str
    violations: tuple[EscapeViolation, ...]
    protected_roots: tuple[Path, ...]
    files_scanned: int

    @property
    def contained(self) -> bool:
        return not self.violations


# ---------------------------------------------------------------------------
# Prevention
# ---------------------------------------------------------------------------


class WriteBarrier:
    """Deny creation of new entries directly inside ``directory``.

    The barrier applies to the directory itself and is not inherited, so an already
    existing subtree — the worktree — stays writable.

    It does **not** protect files that already sit in ``directory``: on Windows a
    ``deny (WD,AD)`` ace blocks *creating* entries, while overwriting an existing file
    is governed by that file's own acl. Verified during the B2 investigation. This is
    why :class:`WorkspaceContainment` requires the barrier directory to hold nothing
    but the worktree.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.mechanism = "not_applied"
        self._previous_mode: int | None = None

    def apply(self) -> str:
        if not self.directory.is_dir():
            raise ContainmentError(f"barrier directory does not exist: {self.directory}")
        self.mechanism = self._apply_windows() if IS_WINDOWS else self._apply_posix()
        return self.mechanism

    def release(self) -> None:
        if self.mechanism == "not_applied":
            return
        if IS_WINDOWS:
            self._run_icacls(["/remove:d", _current_user()])
        elif self._previous_mode is not None:
            self.directory.chmod(self._previous_mode)
        self.mechanism = "not_applied"

    def __enter__(self) -> WriteBarrier:
        self.apply()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    # -- platform implementations ------------------------------------------------

    def _apply_windows(self) -> str:
        # WD = create file, AD = create subdirectory. No (OI)(CI), so the ace covers
        # this folder only and the worktree beneath it remains writable.
        self._run_icacls(["/deny", f"{_current_user()}:(WD,AD)"])
        return "icacls_deny"

    def _apply_posix(self) -> str:
        mode = self.directory.stat().st_mode & 0o7777
        self._previous_mode = mode
        self.directory.chmod(mode & ~0o222)
        return "chmod_ro"

    def _run_icacls(self, arguments: Sequence[str]) -> None:
        completed = subprocess.run(
            ["icacls", str(self.directory), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ContainmentError(
                f"icacls failed on {self.directory}: {completed.stderr.strip()}"
            )


def _current_user() -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        raise ContainmentError("cannot determine the current user for the write barrier")
    return user


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    fingerprints: dict[Path, tuple[int, int]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.fingerprints)


class EscapeDetector:
    """Fingerprints protected paths so post-run drift can be attributed."""

    def __init__(
        self,
        protected_roots: Iterable[Path],
        *,
        allowed_paths: Iterable[Path] = (),
        excluded_dir_names: frozenset[str] = DEFAULT_EXCLUDED_DIR_NAMES,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        self.protected_roots = tuple(sorted({p.resolve() for p in protected_roots}))
        # Narrow, caller-supplied exemptions for writes that are part of the design.
        # A git worktree, for instance, legitimately updates its own metadata and the
        # object store inside the primary checkout; see WorktreeManager.git_allowances.
        # These are still fingerprinted — they are excused at comparison time, so the
        # snapshot stays an honest record of what the tree looked like.
        self.allowed_paths = tuple(sorted({p.resolve() for p in allowed_paths}))
        self.excluded_dir_names = excluded_dir_names
        self.max_files = max_files

    def is_allowed(self, path: Path) -> bool:
        return any(path == allowed or allowed in path.parents for allowed in self.allowed_paths)

    def snapshot(self) -> Snapshot:
        fingerprints: dict[Path, tuple[int, int]] = {}
        for root in self.protected_roots:
            if not root.exists():
                continue
            self._fingerprint_tree(root, fingerprints)
        return Snapshot(fingerprints)

    def compare(self, before: Snapshot, after: Snapshot) -> tuple[EscapeViolation, ...]:
        violations: list[EscapeViolation] = []
        for path, fingerprint in after.fingerprints.items():
            if self.is_allowed(path):
                continue
            previous = before.fingerprints.get(path)
            if previous is None:
                violations.append(EscapeViolation(path, "created"))
            elif previous != fingerprint:
                violations.append(EscapeViolation(path, "modified"))
        for path in before.fingerprints:
            if path not in after.fingerprints and not self.is_allowed(path):
                violations.append(EscapeViolation(path, "deleted"))
        return tuple(sorted(violations, key=lambda v: (v.kind, str(v.path))))

    def _fingerprint_tree(self, root: Path, sink: dict[Path, tuple[int, int]]) -> None:
        for directory, subdirectories, filenames in os.walk(root, onerror=None):
            subdirectories[:] = [d for d in subdirectories if d not in self.excluded_dir_names]
            for filename in filenames:
                path = Path(directory) / filename
                try:
                    stat = path.stat()
                except OSError:
                    # A file we cannot stat is a file we cannot vouch for. Record it
                    # with a sentinel so it still shows up as drift if that changes.
                    sink[path] = (-1, -1)
                    continue
                sink[path] = (stat.st_size, stat.st_mtime_ns)
                if len(sink) > self.max_files:
                    raise ContainmentError(
                        f"protected tree exceeds {self.max_files} files; "
                        "narrow protected_roots or widen excluded_dir_names"
                    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class WorkspaceContainment:
    """Brackets one write-capable run with prevention and detection.

    Usage::

        containment = WorkspaceContainment(worktree, protected_roots=[repo_root])
        containment.arm()
        try:
            ...run the worker...
        finally:
            report = containment.disarm()
        if not report.contained:
            ...fail the run, record report.violations...
    """

    def __init__(
        self,
        worktree: Path,
        protected_roots: Sequence[Path],
        *,
        allowed_paths: Sequence[Path] = (),
        excluded_dir_names: frozenset[str] = DEFAULT_EXCLUDED_DIR_NAMES,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        self.worktree = worktree.resolve()
        self.barrier = WriteBarrier(self.worktree.parent)
        self.detector = EscapeDetector(
            protected_roots,
            allowed_paths=allowed_paths,
            excluded_dir_names=excluded_dir_names,
            max_files=max_files,
        )
        self._before: Snapshot | None = None

    def arm(self) -> None:
        self._validate_layout()
        self._before = self.detector.snapshot()
        self.barrier.apply()

    def disarm(self) -> ContainmentReport:
        if self._before is None:
            raise ContainmentError("disarm() called before arm()")
        mechanism = self.barrier.mechanism
        self.barrier.release()
        after = self.detector.snapshot()
        violations = self.detector.compare(self._before, after)
        return ContainmentReport(
            barrier=mechanism,
            violations=violations,
            protected_roots=self.detector.protected_roots,
            files_scanned=len(after),
        )

    def _validate_layout(self) -> None:
        if not self.worktree.is_dir():
            raise ContainmentError(f"worktree does not exist: {self.worktree}")

        for root in self.detector.protected_roots:
            if self.worktree == root or root in self.worktree.parents:
                raise ContainmentError(
                    f"worktree {self.worktree} sits inside protected root {root}; "
                    "every legitimate write would register as an escape"
                )

        # The barrier cannot protect files that already exist beside the worktree, so
        # the run directory must contain the worktree and nothing else.
        siblings = [entry for entry in self.worktree.parent.iterdir() if entry != self.worktree]
        if siblings:
            raise ContainmentError(
                f"barrier directory {self.worktree.parent} must contain only the worktree; "
                f"found {[entry.name for entry in siblings]}"
            )
