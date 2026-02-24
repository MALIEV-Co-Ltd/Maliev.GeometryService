from fastapi.testclient import TestClient

from src.main import app

# We override the lifespan to avoid starting the real consumer (RabbitMQ dependency)
app.router.lifespan_context = None

client = TestClient(app)


def test_liveness():
    response = client.get("/geometry/liveness")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness():
    response = client.get("/geometry/readiness")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_aspire_liveness():
    response = client.get("/geometry/aspire-liveness")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_telemetry_test():
    response = client.get("/geometry/telemetry-test")
    assert response.status_code == 200
    assert "message" in response.json()


def test_root_health():
    response = client.get("/liveness")
    assert response.status_code == 200
