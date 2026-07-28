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


def test_verification_can_lead_into_a_following_node() -> None:
    """The shipped workflow puts independent review after verify."""
    assert TaskStateMachine.can_transition(TaskState.VERIFYING, TaskState.READY)


def test_verification_is_never_the_end_of_a_run() -> None:
    assert not TaskStateMachine.can_transition(TaskState.VERIFYING, TaskState.COMPLETED)


def test_only_an_approval_can_complete_a_run() -> None:
    completers = [
        state for state in TaskState if TaskStateMachine.can_transition(state, TaskState.COMPLETED)
    ]
    assert completers == [TaskState.WAITING_APPROVAL]
