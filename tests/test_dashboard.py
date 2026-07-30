"""The operator dashboard, and the one endpoint it needed that did not exist.

These tests cover the mount and the workflow endpoint rather than the page. What the
browser paints is not asserted here — there is no JavaScript test runner in this
repository and ADR-011 does not put one in scope. What is asserted is the part a Python
change can break: that the dashboard is reachable, that it is served from the same
origin as the API, that mounting it did not shadow anything, and that the graph it
draws comes from the workflow the runner would actually execute.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.app.main import DASHBOARD_ROOT, app
from apps.api.app.services.orchestration import get_service

client = TestClient(app)

#: Every module the page loads. app.js imports them, so a rename that forgets one
#: leaves a page that 404s at runtime and passes every Python test.
ASSETS = (
    "app.js",
    "api.js",
    "dom.js",
    "graph.js",
    "icons.js",
    "panels.js",
    "runstate.js",
    "views.js",
    "style.css",
)


def test_dashboard_root_serves_the_page() -> None:
    response = client.get("/dashboard/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "AI Work Orchestrator" in response.text


def test_every_asset_the_page_references_is_served() -> None:
    for asset in ASSETS:
        assert client.get(f"/dashboard/{asset}").status_code == 200, asset


def test_modules_are_served_as_javascript() -> None:
    """A browser refuses `type="module"` unless the MIME type is a JavaScript one.

    Worth asserting rather than assuming: StaticFiles asks `mimetypes`, which on
    Windows consults the registry, and a machine where `.js` is registered as
    `text/plain` would serve every module with a 200 and a blank page. That failure is
    invisible from Python otherwise.
    """
    for asset in ASSETS:
        if not asset.endswith(".js"):
            continue
        content_type = client.get(f"/dashboard/{asset}").headers["content-type"]
        assert content_type.split(";")[0] in {"text/javascript", "application/javascript"}, (
            f"{asset} served as {content_type}"
        )


def test_every_module_imported_is_a_module_that_exists() -> None:
    """Follow the import graph. A typo in a relative import is silent until page load."""
    for asset in ASSETS:
        if not asset.endswith(".js"):
            continue
        source = (DASHBOARD_ROOT / asset).read_text(encoding="utf-8")
        for line in source.splitlines():
            if 'from "./' not in line:
                continue
            target = line.split('from "./')[1].split('"')[0]
            assert (DASHBOARD_ROOT / target).is_file(), f"{asset} imports missing {target}"


def test_dashboard_does_not_shadow_the_api() -> None:
    assert client.get("/health").status_code == 200
    # The dashboard calls this one on every load; a mount that swallowed /api/v1 would
    # break the page in exactly the way a static file server can.
    assert client.get("/api/v1/system/capabilities").status_code == 200


def test_unknown_dashboard_path_is_not_a_file_reader() -> None:
    assert client.get("/dashboard/../api/app/config.py").status_code == 404
    assert client.get("/dashboard/nope.js").status_code == 404


def test_dashboard_root_is_inside_apps_web() -> None:
    # The mount resolves a path relative to this file's location. A refactor that moves
    # main.py changes what is published, which is worth failing loudly over.
    assert DASHBOARD_ROOT.is_dir()
    assert DASHBOARD_ROOT == Path(__file__).resolve().parent.parent / "apps" / "web" / "static"


def test_no_cors_middleware_is_installed() -> None:
    """Same-origin is the reason this dashboard is served here at all.

    ADR-011: the API has no authentication, so an added CORS middleware would let any
    page the operator has open drive a control plane that runs CLI agents with write
    access. If CORS is ever genuinely needed, this test should fail and be replaced by
    an explicit decision, not deleted quietly.
    """
    installed = {middleware.cls.__name__ for middleware in app.user_middleware}
    assert "CORSMiddleware" not in installed


# ---------------------------------------------------------------------------
# GET /workflows/{id} — the graph the DAG is drawn from
# ---------------------------------------------------------------------------


def test_workflow_graph_matches_the_configured_workflow() -> None:
    workflow = get_service().workflow
    response = client.get(f"/api/v1/workflows/{workflow.workflow_id}")
    assert response.status_code == 200

    payload = response.json()
    assert payload["workflow_id"] == workflow.workflow_id
    assert payload["version"] == workflow.version
    assert payload["max_repair_rounds"] == workflow.max_repair_rounds
    assert [node["id"] for node in payload["nodes"]] == [node.id for node in workflow.nodes]
    assert payload["execution_order"] == list(workflow.execution_order())


def test_workflow_graph_reports_the_things_a_run_record_cannot() -> None:
    """The point of the endpoint: nodes that have not run, and how they connect."""
    workflow = get_service().workflow
    payload = client.get(f"/api/v1/workflows/{workflow.workflow_id}").json()
    nodes = {node["id"]: node for node in payload["nodes"]}

    # Dependencies, so the graph has edges rather than a bare sequence.
    assert any(node["depends_on"] for node in nodes.values())
    # Which node is a human gate, without the client knowing the magic string.
    assert any(node["is_human"] for node in nodes.values())
    # Which node writes, so the containment boundary is visible in the picture.
    assert any(node["needs_worktree"] for node in nodes.values())
    # And a total, which is the denominator progress needs.
    assert len(nodes) == len(workflow.nodes)


def test_unknown_workflow_is_a_404_not_a_registry_lookup() -> None:
    response = client.get("/api/v1/workflows/not-configured")
    assert response.status_code == 404
    assert "configured" in response.json()["detail"]


def test_workflow_endpoint_exposes_no_run_state() -> None:
    """ADR-012's boundary: this reads configuration and nothing else.

    A run id, a task id or an artifact appearing in this payload would mean the
    endpoint had grown past what was agreed.
    """
    workflow = get_service().workflow
    payload = client.get(f"/api/v1/workflows/{workflow.workflow_id}").json()
    forbidden = {"run_id", "task_id", "completed_nodes", "artifacts", "task_state", "sessions"}
    assert forbidden.isdisjoint(payload)
    for node in payload["nodes"]:
        assert forbidden.isdisjoint(node)
