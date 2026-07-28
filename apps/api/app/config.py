from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://orchestrator:orchestrator@localhost:5432/orchestrator"
    artifact_root: Path = Path("./artifacts")
    run_root: Path = Path("./runs")
    worktree_root: Path = Path("./worktrees")
    default_task_timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    max_repair_rounds: int = Field(default=2, ge=0, le=5)
    allow_network_access: bool = False
    allow_git_push: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
