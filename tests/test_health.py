from fastapi.testclient import TestClient

from apps.api.app.main import app


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ai-work-orchestrator-api",
        "version": "0.1.0",
    }
