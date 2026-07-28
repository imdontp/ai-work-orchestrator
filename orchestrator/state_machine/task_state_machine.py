from orchestrator.domain.models import TaskState


class InvalidTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {TaskState.READY, TaskState.CANCELLED, TaskState.BLOCKED},
    TaskState.READY: {TaskState.RUNNING, TaskState.CANCELLED, TaskState.BLOCKED},
    TaskState.RUNNING: {
        TaskState.VERIFYING,
        TaskState.WAITING_APPROVAL,
        TaskState.FAILED_RETRYABLE,
        TaskState.FAILED_PERMANENT,
        TaskState.CANCELLED,
        TaskState.INTERRUPTED,
    },
    TaskState.VERIFYING: {
        TaskState.WAITING_APPROVAL,
        TaskState.FAILED_RETRYABLE,
        TaskState.FAILED_PERMANENT,
        TaskState.BLOCKED,
    },
    TaskState.WAITING_APPROVAL: {
        TaskState.COMPLETED,
        TaskState.READY,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
    },
    TaskState.FAILED_RETRYABLE: {
        TaskState.READY,
        TaskState.FAILED_PERMANENT,
        TaskState.CANCELLED,
    },
    TaskState.INTERRUPTED: {
        TaskState.READY,
        TaskState.FAILED_PERMANENT,
        TaskState.CANCELLED,
    },
    TaskState.BLOCKED: {TaskState.READY, TaskState.CANCELLED},
    TaskState.COMPLETED: set(),
    TaskState.FAILED_PERMANENT: set(),
    TaskState.CANCELLED: set(),
}


class TaskStateMachine:
    @staticmethod
    def can_transition(current: TaskState, target: TaskState) -> bool:
        return target in _ALLOWED_TRANSITIONS[current]

    @staticmethod
    def transition(current: TaskState, target: TaskState) -> TaskState:
        if not TaskStateMachine.can_transition(current, target):
            raise InvalidTransition(f"Cannot transition task from {current} to {target}")
        return target
