from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: apps/api/app/config.py -> apps/api/app -> apps/api -> apps -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Workers write here, so it must sit outside the primary checkout. See ADR-010.
DEFAULT_WORKTREE_ROOT = Path.home() / ".ai-work-orchestrator" / "workspaces"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://orchestrator:orchestrator@localhost:5432/orchestrator"
    artifact_root: Path = Path("./artifacts")
    run_root: Path = Path("./runs")
    worktree_root: Path = DEFAULT_WORKTREE_ROOT
    default_task_timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    max_repair_rounds: int = Field(default=2, ge=0, le=5)
    allow_network_access: bool = False
    allow_git_push: bool = False

    @field_validator("worktree_root")
    @classmethod
    def worktree_root_must_be_outside_the_checkout(cls, value: Path) -> Path:
        """Reject a workspace root the orchestrator's own source lives in.

        ADR-010: the M1 spike observed a worker writing above its workspace root, so a
        worktree nested in the repository puts the primary checkout one `..` away. This
        is a startup failure rather than a warning because the whole containment design
        rests on the workspace and the checkout being disjoint.
        """
        resolved = value.expanduser().resolve()
        if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
            raise ValueError(
                f"WORKTREE_ROOT must be outside the repository ({REPO_ROOT}); got {resolved}. "
                "Workers write there and a relative escape would reach the primary checkout."
            )
        return resolved


@lru_cache
def get_settings() -> Settings:
    return Settings()
