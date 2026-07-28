import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    timed_out: bool


class ProcessManager:
    """Safe baseline process runner.

    This class is provider-agnostic. Provider adapters are responsible for building
    validated argument lists. Shell strings are intentionally not accepted.
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
        if not args:
            raise ValueError("args must not be empty")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

        cwd = cwd.resolve(strict=True)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=merged_env,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            try:
                exit_code = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
                return ProcessResult(exit_code, stdout_path, stderr_path, False)
            except TimeoutError:
                self._terminate_process_group(process.pid)
                await process.wait()
                return ProcessResult(process.returncode or -1, stdout_path, stderr_path, True)

    @staticmethod
    def _terminate_process_group(process_id: int) -> None:
        try:
            os.killpg(process_id, signal.SIGTERM)
        except ProcessLookupError:
            return
