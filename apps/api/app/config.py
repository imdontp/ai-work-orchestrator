from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    #: "filesystem" or "postgres". Filesystem is the default because it needs nothing
    #: running; postgres is what docs/SYSTEM_ARCHITECTURE.md targets. Artifact payloads
    #: and worker logs stay on disk either way.
    run_store_backend: Literal["filesystem", "postgres"] = "filesystem"
    artifact_root: Path = Path("./artifacts")
    run_root: Path = Path("./runs")
    worktree_root: Path = DEFAULT_WORKTREE_ROOT
    default_task_timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    max_repair_rounds: int = Field(default=2, ge=0, le=5)
    allow_network_access: bool = False
    allow_git_push: bool = False

    #: The repository workers operate on. Unset means the API can describe itself but
    #: cannot start a run - which is the honest default, since guessing a checkout to
    #: point agents at is not a decision to make implicitly.
    project_root: Path | None = None
    #: Workflow graph a submitted task is run through.
    workflow_path: Path = REPO_ROOT / "workflows" / "analyze-implement-review.yaml"
    prompt_root: Path = REPO_ROOT / "prompts"
    contracts_root: Path = REPO_ROOT / "contracts"
    #: Commands re-run mechanically after a write node, as argv lists. Empty means a
    #: run proves nothing and the verify node fails; see WorkflowRunner.
    #: VERIFICATION_COMMANDS='[["python","-m","pytest","-q"]]'
    verification_commands: list[list[str]] = Field(default_factory=list)
    #: Model per worker requirement, e.g. WORKER_MODELS='{"claude_code":"sonnet"}'.
    #: Keyed because a model name means something to exactly one provider.
    worker_models: dict[str, str] = Field(default_factory=dict)

    @field_validator("project_root")
    @classmethod
    def project_root_must_not_be_this_repository(cls, value: Path | None) -> Path | None:
        """Refuse to point workers at the orchestrator's own checkout.

        Workers write to a worktree of whatever PROJECT_ROOT names, and the escape
        detection in ADR-010 treats that repository as the protected root. Aiming it
        at this repository would put the control plane inside its own blast radius.
        """
        if value is None:
            return None
        resolved = value.expanduser().resolve()
        if resolved == REPO_ROOT:
            raise ValueError(
                f"PROJECT_ROOT must not be the orchestrator's own repository ({REPO_ROOT})"
            )
        return resolved

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
