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


def test_client_runtime_manifest_is_public_and_not_cached():
    response = client.get("/geometry/client-runtime/manifest.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"

    body = response.json()
    assert body["runtimeVersion"] == "0.1.0"
    assert body["algorithmVersion"] == "mesh-advisory-v1"
    assert body["authority"] == "advisory"
    assert body["assets"]["worker"].startswith(
        "/geometry/client-runtime/assets/client-geometry-runtime."
    )
    assert body["assets"]["worker"].endswith(".worker.js")
    assert body["capabilities"] == {
        "meshBuffers": True,
        "binaryStl": True,
        "asciiStl": True,
        "cadBrep": False,
        "serverArtifacts": False,
    }


def test_client_runtime_worker_asset_is_hash_named_and_immutable():
    manifest = client.get("/geometry/client-runtime/manifest.json").json()
    asset_url = manifest["assets"]["worker"]

    response = client.get(asset_url)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-type"].startswith("text/javascript")
    assert "MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION" in response.text


def test_client_runtime_missing_asset_returns_404():
    response = client.get(
        "/geometry/client-runtime/assets/client-geometry-runtime.deadbeef.worker.js"
    )

    assert response.status_code == 404
