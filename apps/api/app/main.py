from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from apps.api.app.config import get_settings
from apps.api.app.routers.health import router as health_router
from apps.api.app.routers.runs import router as runs_router
from apps.api.app.routers.system import router as system_router
from apps.api.app.routers.workflows import router as workflows_router

settings = get_settings()

#: apps/api/app/main.py -> apps/api/app -> apps/api -> apps.
DASHBOARD_ROOT = Path(__file__).resolve().parents[2] / "web" / "static"

app = FastAPI(
    title="Personal AI Work Orchestrator API",
    version="0.1.0",
    description="Local-first control plane for CLI agent orchestration.",
)

app.include_router(health_router)
app.include_router(system_router, prefix="/api/v1")
app.include_router(runs_router, prefix="/api/v1")
app.include_router(workflows_router, prefix="/api/v1")

# The operator dashboard is served by this app rather than by a second process, so it
# is same-origin with the API it calls. That is a security decision, not a packaging
# one: the control plane has no authentication, and a cross-origin dashboard would
# need CORS - which would make an unauthenticated API that runs CLI agents with
# filesystem write access reachable from any page the operator happens to have open.
# See ADR-011 and docs/SECURITY_POLICY.md. Mounted under a prefix, so it can never
# shadow /api/v1 or /health.
app.mount(
    "/dashboard",
    StaticFiles(directory=DASHBOARD_ROOT, html=True),
    name="dashboard",
)
