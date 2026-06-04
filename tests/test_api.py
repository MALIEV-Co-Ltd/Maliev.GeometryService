from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi.testclient import TestClient

from src import main
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


class _RecordingCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []

    def add(self, amount: int, attributes: dict[str, Any] | None = None) -> None:
        self.calls.append((amount, attributes or {}))


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
    assert response.headers["x-maliev-geometry-execution-mode"] == (
        "primary_interactive"
    )
    assert response.headers["x-maliev-geometry-authority"] == "local_primary"
    assert response.headers["x-maliev-geometry-server-role"] == (
        "fallback_and_final_validation"
    )

    body = response.json()
    assert body["manifestVersion"] == 1
    assert body["runtimeVersion"] == "1.0.0"
    assert body["algorithmVersion"] == "browser-first-dfm-v1"
    assert body["runtimeKind"] == "browser-first-geometry"
    assert body["executionMode"] == "primary_interactive"
    assert body["authority"] == "local_primary"
    assert body["isAuthoritative"] is False
    assert body["serverRole"] == "fallback_and_final_validation"
    assert body["fallbackPolicy"] == {
        "fallbackOnUnsupportedDevice": True,
        "fallbackOnTimeout": True,
        "fallbackOnInputTooLarge": True,
        "finalValidationRequired": True,
    }
    assert body["assets"]["worker"].startswith(
        "/geometry/client-runtime/assets/client-geometry-runtime."
    )
    assert body["assets"]["worker"].endswith(".worker.js")
    assert body["assets"]["wasm"].startswith(
        "/geometry/client-runtime/assets/client-geometry-kernel."
    )
    assert body["assets"]["wasm"].endswith(".wasm")
    assert body["artifactPolicy"] == {
        "directBrowserViewerExtensions": [".glb", ".gltf", ".obj", ".stl"],
        "browserViewableUploads": {
            "viewerSource": "original_upload",
            "serverEagerMetrics": False,
            "serverGlbExport": False,
            "serverPreviewImages": False,
        },
        "serverGeneratedViewerUploads": {
            "viewerSource": "generated_glb",
            "serverEagerMetrics": True,
            "serverGlbExport": True,
            "serverPreviewImages": True,
        },
    }
    assert body["capabilities"]["inputs"] == {
        "meshBuffers": True,
        "binaryStl": True,
        "asciiStl": True,
        "obj": True,
        "glb": True,
        "gltf": True,
    }
    assert body["capabilities"]["localOperations"] == [
        "mesh_metrics",
        "manifold_check",
        "thin_feature_screening",
        "process_dfm_screening",
        "local_overlay_hints",
    ]
    assert body["capabilities"]["serverOperations"] == [
        "authoritative_dfm",
        "durable_glb_artifacts",
        "durable_preview_images",
        "final_quote_validation",
    ]
    assert body["deviceProfiles"] == {
        "mobile": {
            "maxInputBytes": 8_388_608,
            "maxTriangles": 75_000,
            "timeoutMs": 8_000,
        },
        "tablet": {
            "maxInputBytes": 16_777_216,
            "maxTriangles": 150_000,
            "timeoutMs": 12_000,
        },
        "desktop": {
            "maxInputBytes": 33_554_432,
            "maxTriangles": 350_000,
            "timeoutMs": 20_000,
        },
    }


def test_client_runtime_manifest_emits_browser_primary_offload_decision(monkeypatch):
    counter = _RecordingCounter()
    monkeypatch.setattr(
        main,
        "_GEOMETRY_OFFLOAD_DECISION_COUNTER",
        counter,
        raising=False,
    )

    response = client.get("/geometry/client-runtime/manifest.json")

    assert response.status_code == 200
    assert counter.calls == [
        (
            1,
            {
                "decision": "runtime_delivered",
                "execution_path": "browser_primary",
                "execution_mode": "primary_interactive",
                "authority": "local_primary",
                "server_cpu": "avoided",
                "asset_type": "manifest",
            },
        )
    ]


def test_client_runtime_worker_asset_is_hash_named_and_immutable():
    manifest = client.get("/geometry/client-runtime/manifest.json").json()
    asset_url = manifest["assets"]["worker"]

    response = client.get(asset_url)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-maliev-geometry-execution-mode"] == (
        "primary_interactive"
    )
    assert response.headers["x-maliev-geometry-authority"] == "local_primary"
    assert response.headers["x-maliev-geometry-server-role"] == (
        "fallback_and_final_validation"
    )
    assert "MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION" in response.text
    assert "primary_interactive" in response.text


def test_client_runtime_wasm_asset_is_hash_named_and_immutable():
    manifest = client.get("/geometry/client-runtime/manifest.json").json()
    asset_url = manifest["assets"]["wasm"]

    response = client.get(asset_url)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-type"] == "application/wasm"
    assert response.headers["x-maliev-geometry-execution-mode"] == (
        "primary_interactive"
    )
    assert response.headers["x-maliev-geometry-authority"] == "local_primary"
    assert response.headers["x-maliev-geometry-server-role"] == (
        "fallback_and_final_validation"
    )
    assert response.content.startswith(b"\x00asm")


def test_client_runtime_missing_asset_returns_404():
    response = client.get(
        "/geometry/client-runtime/assets/client-geometry-runtime.deadbeef.worker.js"
    )

    assert response.status_code == 404


def test_server_dfm_response_emits_server_fallback_offload_decision(monkeypatch):
    counter = _RecordingCounter()
    monkeypatch.setattr(
        main,
        "_GEOMETRY_OFFLOAD_DECISION_COUNTER",
        counter,
        raising=False,
    )

    response = client.post(
        "/geometry/uploads/test-upload-no-quality/dfm/FDM",
        headers=_auth_headers(),
    )

    assert response.status_code == 404
    assert counter.calls == [
        (
            1,
            {
                "decision": "required",
                "execution_path": "server_fallback",
                "execution_mode": "server_fallback_final_validation",
                "authority": "server_authoritative",
                "server_cpu": "consumed",
                "process_code": "FDM",
                "status_code": 404,
                "cache_status": "none",
            },
        )
    ]
