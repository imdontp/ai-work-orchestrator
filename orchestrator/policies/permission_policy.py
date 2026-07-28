from dataclasses import dataclass

from orchestrator.domain.models import ApprovalRisk, TaskPermissions


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    risk: ApprovalRisk
    reason: str


class PermissionPolicy:
    def evaluate(self, action: str, permissions: TaskPermissions) -> PolicyDecision:
        if action == "git_push":
            return PolicyDecision(False, True, ApprovalRisk.HIGH, "Git push is disabled in MVP")
        if action == "network_access" and not permissions.network:
            return PolicyDecision(False, False, ApprovalRisk.HIGH, "Network access was not granted")
        if action == "write_files" and permissions.filesystem != "scoped_write":
            return PolicyDecision(False, False, ApprovalRisk.MEDIUM, "Task is read-only")
        if action == "read_secrets" and not permissions.secrets:
            return PolicyDecision(False, False, ApprovalRisk.HIGH, "Secret access was not granted")
        return PolicyDecision(True, False, ApprovalRisk.LOW, "Allowed by task permissions")
