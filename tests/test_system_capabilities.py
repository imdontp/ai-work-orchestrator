"""The capabilities endpoint must describe what is actually implemented.

It reported `M0_FOUNDATION` and two `_planned` workers for the whole of M1, long after
both adapters existed. These tests tie the payload to the adapter registry and the
settings so it cannot drift silently again.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.app.config import REPO_ROOT
from apps.api.app.main import app
from workers import ADAPTERS_BY_NAME


def _capabilities() -> dict:
    response = TestClient(app).get("/api/v1/system/capabilities")
    assert response.status_code == 200
    return response.json()


def test_configured_workers_match_the_adapter_registry() -> None:
    assert _capabilities()["configured_workers"] == sorted(ADAPTERS_BY_NAME)


def test_no_worker_is_advertised_as_merely_planned() -> None:
    assert not [w for w in _capabilities()["configured_workers"] if w.endswith("_planned")]


def test_workspace_root_is_reported_and_sits_outside_the_checkout() -> None:
    """ADR-010: containment fails if worker worktrees live inside the repository."""
    workspace_root = Path(_capabilities()["workspace_root"])

    assert workspace_root.is_absolute()
    assert workspace_root != REPO_ROOT
    assert REPO_ROOT not in workspace_root.parents


def test_dangerous_operations_are_denied_by_default() -> None:
    payload = _capabilities()

    assert payload["git_push_allowed"] is False
    assert payload["network_access_allowed"] is False
