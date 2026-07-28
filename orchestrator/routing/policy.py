from dataclasses import dataclass

from orchestrator.domain.models import ExecutionMode


@dataclass(frozen=True)
class RoutingInput:
    task_type: str
    complexity: str
    requires_write: bool
    requires_independent_review: bool


class StaticRoutingPolicy:
    """Transparent baseline router for the foundation milestone."""

    def select_mode(self, data: RoutingInput) -> ExecutionMode:
        if data.requires_write and data.requires_independent_review:
            return ExecutionMode.REPAIR_LOOP
        if data.complexity in {"high", "very_high"}:
            return ExecutionMode.PIPELINE
        return ExecutionMode.DIRECT
