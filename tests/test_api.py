from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

from src.core.config import settings
from src.main import app

SECURITY_KEY = "a-very-long-test-secret-key-at-least-32-chars!"
ISSUER = "https://api.test.maliev.com"
AUDIENCE = "https://api.test.maliev.com"

settings.ASPNETCORE_ENVIRONMENT = "Testing"
settings.JWT_SECURITY_KEY = SECURITY_KEY
settings.JWT_ISSUER = ISSUER
settings.JWT_AUDIENCE = AUDIENCE

client = TestClient(app)


def _auth_headers() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "test-user",
            "permissions": "geometry.analysis.run",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
        },
        SECURITY_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


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


def test_telemetry_test_requires_auth():
    response = client.get("/geometry/telemetry-test")
    assert response.status_code == 401


def test_quality_check_requires_auth():
    response = client.post(
        "/geometry/uploads/test-upload-auth/quality-check",
        json={"stl_bytes": ""},
    )
    assert response.status_code == 401


def test_telemetry_test_with_auth():
    response = client.get("/geometry/telemetry-test", headers=_auth_headers())
    assert response.status_code == 200
    assert "message" in response.json()


def test_root_health():
    response = client.get("/liveness")
    assert response.status_code == 200
