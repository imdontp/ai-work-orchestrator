from fastapi import APIRouter
from pydantic import BaseModel

from apps.api.app.config import get_settings

router = APIRouter(tags=["system"])


class SystemCapabilities(BaseModel):
    milestone: str
    execution_modes: list[str]
    configured_workers: list[str]
    git_push_allowed: bool
    network_access_allowed: bool


@router.get("/system/capabilities", response_model=SystemCapabilities)
def capabilities() -> SystemCapabilities:
    settings = get_settings()
    return SystemCapabilities(
        milestone="M0_FOUNDATION",
        execution_modes=["direct", "pipeline", "repair_loop"],
        configured_workers=["claude_code_planned", "codex_planned"],
        git_push_allowed=settings.allow_git_push,
        network_access_allowed=settings.allow_network_access,
    )
