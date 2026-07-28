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

#: Worker names as they appear in `worker_requirement` in the workflow graphs.
ADAPTERS_BY_NAME = {
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
