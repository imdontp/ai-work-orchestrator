"""Shared plumbing for adapters that drive a local CLI worker.

Provider specifics stay in the subclasses: this class never knows a flag name. It owns
the parts that must not be reimplemented per provider — spawning through
:class:`~execution.process_manager.ProcessManager` so the verified process-tree
termination applies everywhere, tailing stdout for live events, and normalizing the
outcome into a :class:`~workers.base.WorkerResult`.

Subclasses implement three hooks:

``build_argv``      turn a request into an argv list (never a shell string)
``parse_output``    turn the captured stdout into a :class:`CliOutcome`
``health_check``    report version, auth mode and flag-surface agreement
"""

from __future__ import annotations

import asyncio
import json
import shutil
from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from execution.process_manager import ProcessHandle, ProcessManager, ProcessResult
from workers.base import WorkerAdapter, WorkerHandle, WorkerRequest, WorkerResult

#: Poll interval while tailing a worker's stdout for events.
STREAM_POLL_SECONDS = 0.05


class WorkerAdapterError(RuntimeError):
    """The adapter could not run, or was asked about a run it does not own."""


@dataclass(frozen=True)
class CliOutcome:
    """What the adapter could establish from a worker's own output."""

    session_id: str | None = None
    result_text: str | None = None
    structured_result: Any = None
    reported_error: bool = False
    #: "auth", "quota", "model", "usage", "runtime" or "no_result".
    error_kind: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "result_text": self.result_text,
            "structured_result": self.structured_result,
            "reported_error": self.reported_error,
            "error_kind": self.error_kind,
            "usage": self.usage,
        }


@dataclass
class _Run:
    request: WorkerRequest
    handle: ProcessHandle
    waiter: asyncio.Task[ProcessResult]
    stdout_path: Path
    stderr_path: Path
    outcome_path: Path


class CliWorkerAdapter(WorkerAdapter):
    """A WorkerAdapter backed by a local command-line worker."""

    #: Subcommand-scoped flags the adapter depends on. ``health_check`` asserts each is
    #: still present, because a CLI upgrade can silently retire one. This is not
    #: hypothetical: the M1 spike lost 12 of 15 probes to ``-a`` existing on ``codex``
    #: but not on ``codex exec``.
    required_flags: tuple[str, ...] = ()

    def __init__(
        self,
        executable: Path | None = None,
        process_manager: ProcessManager | None = None,
    ) -> None:
        self._executable = executable
        self._process_manager = process_manager or ProcessManager()
        self._runs: dict[str, _Run] = {}

    # -- provider hooks ----------------------------------------------------------

    @abstractmethod
    def build_argv(self, request: WorkerRequest) -> list[str]:
        """Return the full argv for this request. Must not include a shell."""
        raise NotImplementedError

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str) -> CliOutcome:
        """Normalize the worker's own output. Must not raise on malformed input."""
        raise NotImplementedError

    @abstractmethod
    def resolve_executable(self) -> Path:
        """Locate the CLI binary, preferring one that needs no shell to launch."""
        raise NotImplementedError

    # -- lifecycle ---------------------------------------------------------------

    @property
    def executable(self) -> Path:
        if self._executable is None:
            self._executable = self.resolve_executable()
        return self._executable

    async def start(self, request: WorkerRequest) -> WorkerHandle:
        request.log_dir.mkdir(parents=True, exist_ok=True)
        worker_run_id = f"{request.run_id}-{self.name}-{uuid4().hex[:8]}"

        stdout_path = request.log_dir / f"{worker_run_id}.stdout.jsonl"
        stderr_path = request.log_dir / f"{worker_run_id}.stderr.log"
        outcome_path = request.log_dir / f"{worker_run_id}.outcome.json"

        handle = await self._process_manager.start(
            args=self.build_argv(request),
            cwd=request.workspace,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            env=request.environment,
        )
        waiter = asyncio.create_task(
            self._process_manager.wait(handle, timeout_seconds=request.timeout_seconds)
        )

        self._runs[worker_run_id] = _Run(
            request=request,
            handle=handle,
            waiter=waiter,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            outcome_path=outcome_path,
        )
        return WorkerHandle(worker_run_id=worker_run_id, process_id=handle.pid)

    async def stream_events(self, handle: WorkerHandle) -> AsyncIterator[str]:
        """Yield stdout lines as the worker produces them.

        Reads from the log file rather than the pipe so the same bytes reach the
        orchestrator and the audit trail, and so a slow consumer cannot stall the
        worker by failing to drain a pipe.
        """
        run = self._run_for(handle)
        position = 0
        buffer = b""

        while True:
            finished = run.waiter.done()
            chunk = b""
            if run.stdout_path.exists():
                with run.stdout_path.open("rb") as stream:
                    stream.seek(position)
                    chunk = stream.read()
                    position = stream.tell()

            buffer += chunk
            while b"\n" in buffer:
                raw, _, buffer = buffer.partition(b"\n")
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if line:
                    yield line

            if finished and not chunk:
                break
            await asyncio.sleep(STREAM_POLL_SECONDS)

        trailing = buffer.decode("utf-8", "replace").strip()
        if trailing:
            yield trailing

    async def cancel(self, handle: WorkerHandle) -> None:
        await self._process_manager.cancel(self._run_for(handle).handle)

    async def collect(self, handle: WorkerHandle) -> WorkerResult:
        run = self._run_for(handle)
        result = await run.waiter

        stdout = _read_text(run.stdout_path)
        stderr = _read_text(run.stderr_path)
        outcome = self.parse_output(stdout, stderr)

        if result.timed_out or result.cancelled:
            # A killed process has no terminal event, so its own output cannot say
            # what happened. The process result is the authority here.
            outcome = CliOutcome(
                session_id=outcome.session_id,
                result_text=outcome.result_text,
                structured_result=outcome.structured_result,
                reported_error=True,
                error_kind="cancelled" if result.cancelled else "timeout",
                usage=outcome.usage,
            )

        payload = {
            "worker": self.name,
            "worker_run_id": handle.worker_run_id,
            "task_id": run.request.task_id,
            "run_id": run.request.run_id,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "termination": result.termination,
            **outcome.to_json(),
        }
        run.outcome_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        del self._runs[handle.worker_run_id]

        return WorkerResult(
            exit_code=result.exit_code,
            stdout_path=result.stdout_path,
            stderr_path=result.stderr_path,
            result_path=run.outcome_path,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            session_id=outcome.session_id,
            reported_error=outcome.reported_error,
            error_kind=outcome.error_kind,
        )

    # -- helpers -----------------------------------------------------------------

    def _run_for(self, handle: WorkerHandle) -> _Run:
        run = self._runs.get(handle.worker_run_id)
        if run is None:
            raise WorkerAdapterError(f"unknown worker run: {handle.worker_run_id}")
        return run

    async def _capture(self, *arguments: str, timeout: float = 60.0) -> tuple[int, str, str]:
        """Run the CLI and capture its output. For health checks only."""
        process = await asyncio.create_subprocess_exec(
            str(self.executable),
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return -1, "", f"timed out after {timeout}s"
        return (
            process.returncode if process.returncode is not None else -1,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    async def _check_flag_surface(self, *help_arguments: str) -> dict[str, Any]:
        """Confirm the CLI still advertises every flag this adapter passes."""
        exit_code, stdout, stderr = await self._capture(*help_arguments)
        help_text = stdout + stderr
        missing = [flag for flag in self.required_flags if flag not in help_text]
        return {
            "checked": list(self.required_flags),
            "missing": missing,
            "ok": exit_code == 0 and not missing,
        }

    @staticmethod
    def _which(*candidates: str) -> Path | None:
        for candidate in candidates:
            found = shutil.which(candidate)
            if found:
                return Path(found)
        return None


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")
