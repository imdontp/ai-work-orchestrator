from orchestrator.domain.models import TaskPermissions
from orchestrator.policies.permission_policy import PermissionPolicy


def test_git_push_is_denied() -> None:
    policy = PermissionPolicy()
    permissions = TaskPermissions(filesystem="scoped_write")
    decision = policy.evaluate("git_push", permissions)
    assert not decision.allowed
    assert decision.requires_approval


def test_read_only_task_cannot_write() -> None:
    policy = PermissionPolicy()
    permissions = TaskPermissions(filesystem="read_only")
    decision = policy.evaluate("write_files", permissions)
    assert not decision.allowed
