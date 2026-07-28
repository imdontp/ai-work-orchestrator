from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkerCapabilities:
    structured_output: bool
    stream_events: bool
    resume_session: bool
    cancel_process: bool
    scoped_write: bool
    server_mode: bool


@dataclass(frozen=True)
class WorkerRequest:
    task_id: str
    run_id: str
    prompt: str
    workspace: Path
    output_schema_path: Path | None
    timeout_seconds: int
    environment: dict[str, str]


@dataclass(frozen=True)
class WorkerHandle:
    worker_run_id: str
    process_id: int


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    result_path: Path | None
    timed_out: bool = False
    cancelled: bool = False


class WorkerAdapter(ABC):
    name: str
    capabilities: WorkerCapabilities

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def start(self, request: WorkerRequest) -> WorkerHandle:
        raise NotImplementedError

    @abstractmethod
    async def stream_events(self, handle: WorkerHandle) -> AsyncIterator[str]:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, handle: WorkerHandle) -> None:
        raise NotImplementedError

    @abstractmethod
    async def collect(self, handle: WorkerHandle) -> WorkerResult:
        raise NotImplementedError
