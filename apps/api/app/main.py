from fastapi import FastAPI

from apps.api.app.config import get_settings
from apps.api.app.routers.health import router as health_router
from apps.api.app.routers.runs import router as runs_router
from apps.api.app.routers.system import router as system_router

settings = get_settings()

app = FastAPI(
    title="Personal AI Work Orchestrator API",
    version="0.1.0",
    description="Local-first control plane for CLI agent orchestration.",
)

app.include_router(health_router)
app.include_router(system_router, prefix="/api/v1")
app.include_router(runs_router, prefix="/api/v1")
