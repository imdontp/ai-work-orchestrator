from orchestrator.domain.models import ExecutionMode
from orchestrator.routing.policy import RoutingInput, StaticRoutingPolicy


def test_small_read_task_routes_direct() -> None:
    policy = StaticRoutingPolicy()
    mode = policy.select_mode(
        RoutingInput(
            task_type="analysis",
            complexity="low",
            requires_write=False,
            requires_independent_review=False,
        )
    )
    assert mode == ExecutionMode.DIRECT


def test_write_and_review_routes_repair_loop() -> None:
    policy = StaticRoutingPolicy()
    mode = policy.select_mode(
        RoutingInput(
            task_type="implementation",
            complexity="medium",
            requires_write=True,
            requires_independent_review=True,
        )
    )
    assert mode == ExecutionMode.REPAIR_LOOP
