from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


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
    #: The worker's working directory. For a write task this is the worktree, and it
    #: is the only path the worker is expected to modify.
    workspace: Path
    #: Where the adapter writes stdout, stderr and its normalized outcome. Kept out of
    #: the workspace on purpose: logs written into the worktree would show up in the
    #: worker's own diff.
    log_dir: Path
    output_schema_path: Path | None
    timeout_seconds: int
    environment: dict[str, str]
    #: The agent profile's role definition, from prompts/<profile>/system.md. Adapters
    #: apply it however their CLI allows — Claude Code has a flag for it, Codex has
    #: none and gets it prepended to the payload.
    system_prompt: str | None = None
    #: Model alias or full name. ``None`` leaves the choice to the CLI's own default.
    model: str | None = None
    #: Session id the orchestrator assigns to a new session. Claude Code accepts one;
    #: Codex does not, and reports the id it chose instead (see WorkerResult).
    session_id: str | None = None
    #: Resume an existing session or thread instead of starting a new one. Mutually
    #: exclusive with session_id. Review nodes must leave this unset — the workflow
    #: requires a fresh session for independent review.
    resume_from: str | None = None
    #: What the worker is allowed to do with its tools, named in terms every provider
    #: can honour. The adapter maps this to its own CLI's vocabulary — the orchestrator
    #: must not know that Claude Code spells reading "Read,Glob,Grep" while Codex has no
    #: tool flag at all and governs the same thing through its sandbox mode.
    #:
    #: "none"      no tools; the worker may only reason about what it was given
    #: "read"      inspect the workspace, change nothing
    #: "write"     inspect and modify the workspace
    tool_access: Literal["none", "read", "write"] = "read"
    #: "read_only" or "scoped_write", mirroring TaskPermissions.filesystem.
    filesystem_access: str = "read_only"

    def __post_init__(self) -> None:
        if self.session_id and self.resume_from:
            raise ValueError("session_id and resume_from are mutually exclusive")
        if self.filesystem_access not in {"read_only", "scoped_write"}:
            raise ValueError(
                f"filesystem_access must be read_only or scoped_write, "
                f"got {self.filesystem_access!r}"
            )


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
    #: The session this run belongs to, for a later resume. Claude Code echoes the id
    #: we assigned; Codex reports the thread id it chose.
    session_id: str | None = None
    #: True when the worker itself reported failure. Independent of exit_code: Claude
    #: Code returns a JSON envelope with is_error set while still exiting non-zero, and
    #: a usage error produces no envelope at all.
    reported_error: bool = False
    #: Coarse cause when reported_error is set: "auth", "quota", "model", "usage",
    #: "runtime" or "no_result". Lets the orchestrator decide retryability without
    #: parsing provider text.
    error_kind: str | None = None


class WorkerAdapter(ABC):
    name: str
    capabilities: WorkerCapabilities

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def start(self, request: WorkerRequest) -> WorkerHandle:
        raise NotImplementedError

    # Not `async def`: implementations are async generators, whose type is already
    # AsyncIterator[str]. Declaring this async would demand a coroutine *returning* an
    # iterator, which is a different and less useful shape.
    @abstractmethod
    def stream_events(self, handle: WorkerHandle) -> AsyncIterator[str]:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, handle: WorkerHandle) -> None:
        raise NotImplementedError

    @abstractmethod
    async def collect(self, handle: WorkerHandle) -> WorkerResult:
        raise NotImplementedError
