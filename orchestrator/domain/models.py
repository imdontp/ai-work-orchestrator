from enum import StrEnum

from pydantic import BaseModel, Field


class TaskState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class ExecutionMode(StrEnum):
    DIRECT = "direct"
    PIPELINE = "pipeline"
    REPAIR_LOOP = "repair_loop"
    PARALLEL = "parallel"


class ApprovalRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskPermissions(BaseModel):
    filesystem: str = Field(pattern="^(read_only|scoped_write)$")
    network: bool = False
    git_push: bool = False
    secrets: bool = False


class Task(BaseModel):
    schema_version: str = "1.0"
    task_id: str
    project_id: str
    task_type: str
    objective: str
    acceptance_criteria: list[str] = Field(min_length=1)
    permissions: TaskPermissions
    expected_outputs: list[str] = Field(min_length=1)
    state: TaskState = TaskState.PENDING
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=2, ge=1, le=5)
