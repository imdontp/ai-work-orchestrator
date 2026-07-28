import pytest

from orchestrator.domain.models import TaskState
from orchestrator.state_machine import InvalidTransition, TaskStateMachine


def test_valid_transition() -> None:
    assert TaskStateMachine.transition(TaskState.PENDING, TaskState.READY) == TaskState.READY


def test_completed_is_terminal() -> None:
    with pytest.raises(InvalidTransition):
        TaskStateMachine.transition(TaskState.COMPLETED, TaskState.READY)


def test_worker_completion_requires_verification_or_approval() -> None:
    assert not TaskStateMachine.can_transition(TaskState.RUNNING, TaskState.COMPLETED)
