from collections.abc import Callable

from workers.base import (
    WorkerAdapter,
    WorkerCapabilities,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
)
from workers.claude_code import ClaudeCodeAdapter
from workers.cli_base import CliOutcome, CliWorkerAdapter, WorkerAdapterError
from workers.codex import CodexAdapter

#: Worker names as they appear in `worker_requirement` in the workflow graphs, mapped
#: to a factory. Factories rather than classes so callers get a fresh adapter per run
#: and the type stays concrete — `type[WorkerAdapter]` is abstract and not callable.
ADAPTERS_BY_NAME: dict[str, Callable[[], WorkerAdapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CodexAdapter.name: CodexAdapter,
}

__all__ = [
    "ADAPTERS_BY_NAME",
    "ClaudeCodeAdapter",
    "CliOutcome",
    "CliWorkerAdapter",
    "CodexAdapter",
    "WorkerAdapter",
    "WorkerAdapterError",
    "WorkerCapabilities",
    "WorkerHandle",
    "WorkerRequest",
    "WorkerResult",
]
