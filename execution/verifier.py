from dataclasses import dataclass
from pathlib import Path

from execution.process_manager import ProcessManager, ProcessResult


@dataclass(frozen=True)
class VerificationCommand:
    args: list[str]
    timeout_seconds: int = 600


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    command_results: list[ProcessResult]


class VerificationRunner:
    def __init__(self, process_manager: ProcessManager) -> None:
        self._process_manager = process_manager

    async def run(
        self,
        *,
        commands: list[VerificationCommand],
        workspace: Path,
        output_dir: Path,
    ) -> VerificationResult:
        results: list[ProcessResult] = []
        for index, command in enumerate(commands, start=1):
            result = await self._process_manager.run(
                args=command.args,
                cwd=workspace,
                stdout_path=output_dir / f"verify-{index}.stdout.log",
                stderr_path=output_dir / f"verify-{index}.stderr.log",
                timeout_seconds=command.timeout_seconds,
            )
            results.append(result)
            if result.exit_code != 0 or result.timed_out:
                return VerificationResult(False, results)
        return VerificationResult(True, results)
