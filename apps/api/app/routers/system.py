from fastapi import APIRouter
from pydantic import BaseModel

from apps.api.app.config import get_settings
from workers import ADAPTERS_BY_NAME

router = APIRouter(tags=["system"])


class SystemCapabilities(BaseModel):
    milestone: str
    execution_modes: list[str]
    configured_workers: list[str]
    git_push_allowed: bool
    network_access_allowed: bool
    #: Where worker worktrees are created. Reported because it must sit outside the
    #: repository for containment to hold (ADR-010), and that is worth being able to
    #: check from outside the process.
    workspace_root: str


@router.get("/system/capabilities", response_model=SystemCapabilities)
def capabilities() -> SystemCapabilities:
    settings = get_settings()
    return SystemCapabilities(
        milestone="M2_EXECUTION",
        execution_modes=["direct", "pipeline", "repair_loop"],
        # Read from the adapter registry rather than restated here, so this cannot
        # drift from what is actually implemented the way it did through M1.
        configured_workers=sorted(ADAPTERS_BY_NAME),
        git_push_allowed=settings.allow_git_push,
        network_access_allowed=settings.allow_network_access,
        workspace_root=str(settings.worktree_root),
    )
