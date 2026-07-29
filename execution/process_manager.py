import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

IS_WINDOWS = sys.platform == "win32"

# How long a process gets to exit after the graceful signal before it is forced.
TERMINATE_GRACE_SECONDS = 5.0

# How long the forced kill gets to be reaped before we give up waiting.
FORCE_REAP_SECONDS = 10.0


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    timed_out: bool
    #: How the process ended. ``None`` for a natural exit; otherwise the escalation
    #: step that actually stopped it. Recorded so the audit trail distinguishes a
    #: worker that shut down cleanly from one that had to be forced.
    termination: str | None = None
    #: True when the process was stopped by an explicit cancel rather than by its
    #: own deadline. Both produce a dead process; only one is the worker's fault.
    cancelled: bool = False


class ProcessHandle:
    """A running process plus the log files it is writing into.

    Returned by :meth:`ProcessManager.start` for callers that need to observe or
    cancel a process while it runs. Callers that only need the result should use
    :meth:`ProcessManager.run`.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        stdout_path: Path,
        stderr_path: Path,
        open_files: Sequence[IO[bytes]],
    ) -> None:
        self._process = process
        self._open_files = tuple(open_files)
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.cancelled = False
        self.termination: str | None = None
        #: Set by :meth:`ProcessManager.cancel` once it has recorded how it killed the
        #: process. Created before the kill starts, so a concurrent :meth:`wait` — which
        #: the dying process wakes — can tell "no cancel is in flight" from "a cancel is
        #: in flight and has not written its answer yet".
        self.cancel_finished: asyncio.Event | None = None

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def running(self) -> bool:
        return self._process.returncode is None

    def _close_files(self) -> None:
        for handle in self._open_files:
            if not handle.closed:
                handle.close()


class ProcessManager:
    """Safe baseline process runner.

    This class is provider-agnostic. Provider adapters are responsible for building
    validated argument lists. Shell strings are intentionally not accepted.

    Termination is platform-dispatched. Both paths own the whole process tree, not
    just the launched process: a CLI worker that spawns children (a Node shim in
    front of a native binary, a shell it invoked itself) must not survive its own
    timeout. See ``docs/spikes/M1_CLI_CAPABILITY_REPORT.md`` section 5 (B1).
    """

    async def run(
        self,
        *,
        args: list[str],
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        """Start a process and wait for it, enforcing the deadline."""
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        handle = await self.start(
            args=args,
            cwd=cwd,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            env=env,
        )
        return await self.wait(handle, timeout_seconds=timeout_seconds)

    async def start(
        self,
        *,
        args: list[str],
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        env: dict[str, str] | None = None,
    ) -> ProcessHandle:
        """Spawn a process and return immediately.

        The caller owns the returned handle and must finish it with :meth:`wait`,
        which is what closes the log files.
        """
        if not args:
            raise ValueError("args must not be empty")

        cwd = cwd.resolve(strict=True)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        stdout_file = stdout_path.open("wb")
        stderr_file = stderr_path.open("wb")
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=merged_env,
                # Workers must never inherit the orchestrator's stdin. Codex, for one,
                # appends whatever is on a piped stdin to the prompt it was given.
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                **self._spawn_kwargs(),
            )
        except BaseException:
            stdout_file.close()
            stderr_file.close()
            raise

        return ProcessHandle(process, stdout_path, stderr_path, (stdout_file, stderr_file))

    async def wait(self, handle: ProcessHandle, *, timeout_seconds: int) -> ProcessResult:
        """Wait for a started process, killing its tree if it overruns the deadline."""
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

        timed_out = False
        try:
            await asyncio.wait_for(handle._process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            handle.termination = await self._terminate_process_tree(handle._process)
        finally:
            handle._close_files()

        # A cancel kills the process, and the death of the process is what wakes the
        # await above — inside the kill, before it has recorded how it did it. Reading
        # `termination` now would snapshot a None that is about to be filled in. A live
        # run cancelled mid-worker recorded `termination: null` for a process that had
        # in fact been killed by taskkill.
        if handle.cancel_finished is not None:
            await handle.cancel_finished.wait()

        exit_code = handle.returncode
        return ProcessResult(
            exit_code if exit_code is not None else -1,
            handle.stdout_path,
            handle.stderr_path,
            timed_out,
            handle.termination,
            handle.cancelled,
        )

    async def cancel(self, handle: ProcessHandle) -> str:
        """Stop a running process on request rather than on its deadline.

        Safe to call on a process that has already exited. The concurrent
        :meth:`wait` returns normally with ``cancelled`` set, and waits for the
        termination recorded here rather than racing it.
        """
        # Both assignments happen before the first await, so a concurrent `wait` either
        # sees no cancel at all or sees one it can wait for. There is no window where
        # `cancelled` is set but the event is missing.
        handle.cancelled = True
        finished = handle.cancel_finished or asyncio.Event()
        handle.cancel_finished = finished
        try:
            if not handle.running:
                handle.termination = handle.termination or "already_exited"
                return handle.termination
            handle.termination = await self._terminate_process_tree(handle._process)
            return handle.termination
        finally:
            finished.set()

    @staticmethod
    def _spawn_kwargs() -> dict[str, Any]:
        """Isolate the child so it can be signalled without hitting the orchestrator.

        ``Any`` rather than ``object`` because the keys are platform-dependent and get
        splatted into a call with per-argument types. The ``sys.platform`` comparison —
        rather than the ``IS_WINDOWS`` constant — is what lets a type checker mark the
        other branch unreachable, so ``CREATE_NEW_PROCESS_GROUP`` is not flagged as
        missing when checking on POSIX.
        """
        if sys.platform == "win32":
            # Required for GenerateConsoleCtrlEvent to target only the child's group,
            # and it makes the child's PID the root of a killable tree.
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    @classmethod
    async def _terminate_process_tree(cls, process: asyncio.subprocess.Process) -> str:
        if IS_WINDOWS:
            return await cls._terminate_windows(process)
        return await cls._terminate_posix(process)

    @staticmethod
    async def _terminate_posix(process: asyncio.subprocess.Process) -> str:
        """SIGTERM the process group, then SIGKILL it if it outlives the grace period.

        The platform guard is load-bearing for type checking as well as for runtime:
        it makes the rest of this body unreachable when checking on Windows, where
        ``os.killpg`` and ``signal.SIGKILL`` do not exist. Tests that exercise this
        branch off-POSIX patch ``sys.platform`` along with the syscalls.
        """
        if sys.platform == "win32":
            raise RuntimeError("_terminate_posix called on Windows; use _terminate_windows")

        try:
            # The group id, not the pid. They are equal because start_new_session made
            # the child a group leader, but relying on that coincidence silently breaks
            # if the spawn flags ever change.
            group_id = os.getpgid(process.pid)
        except ProcessLookupError:
            return "already_exited"

        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            return "already_exited"

        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
            return "sigterm"
        except TimeoutError:
            pass

        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            return "sigterm"

        await asyncio.wait_for(process.wait(), timeout=FORCE_REAP_SECONDS)
        return "sigkill"

    @staticmethod
    async def _terminate_windows(process: asyncio.subprocess.Process) -> str:
        """Force-kill the whole tree. ``os.killpg`` does not exist on Windows.

        There is deliberately no graceful phase here. ``CTRL_BREAK_EVENT`` only reaches
        children that share a console with the orchestrator, so it can stop the launcher
        while leaving a grandchild running — and once the launcher is reaped its PID is
        no longer a safe handle on the rest of the tree. ``taskkill /F /T`` is the path
        the M1 spike verified: it reclaimed both the Claude Code and the Codex trees with
        no surviving processes. A timed-out worker has already exceeded its budget, so
        trading a flush opportunity for a guarantee of no orphans is the right way round.
        """
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(process.pid),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await killer.wait()

        try:
            await asyncio.wait_for(process.wait(), timeout=FORCE_REAP_SECONDS)
        except TimeoutError:
            return "taskkill_tree_unreaped"
        return "taskkill_tree"
