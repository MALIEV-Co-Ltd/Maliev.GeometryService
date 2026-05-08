"""Unit tests for the UploadService-backed DFM cache.

UploadService HTTP calls are mocked with ``respx`` so these tests run without
any network access.  We verify:

* Key derivation (tol_bucket, flag_bucket, full storage paths)
* Round-trip serialization for the metrics dict (bytes fields → base64 → bytes)
* Disabled-by-flag behavior (no HTTP calls when toggled off)
* Cache-miss path returns ``None`` (covers 410 Gone and 404 endpoint-not-deployed)
* Cache-hit path: signed URL → blob download → deserialized dict
* Failures are absorbed (logged, never re-raised)
* Flag-bucket changes invalidate cache reads (different storage path)
"""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
import respx

from src.infrastructure.upload_cache import (
    _deserialize_metrics_dict,
    _serialize_metrics_dict,
    compute_flag_bucket,
    dfm_result_storage_path,
    get_cached_dfm_result,
    get_cached_tessellation,
    put_dfm_result,
    put_tessellation,
    sha256_of,
    tessellation_storage_path,
    tol_bucket_for,
)


class _FakeTokenProvider:
    def get_token(self) -> str:
        return "fake-token-xyz"


@pytest.fixture
def clean_settings():
    """Snapshot and restore the settings fields these tests mutate.

    pytest's ``monkeypatch.setattr`` does not always restore pydantic-settings
    fields cleanly when other tests touch the same instance, so we save/restore
    explicitly via ``try/finally``.  Keeps tests deterministic regardless of
    which other test modules ran before us in the suite.
    """
    from src.core.config import settings

    fields = (
        "USE_BREP_THICKNESS",
        "USE_SDF_SMALL_FEATURES",
        "GEOMETRY_TESSELLATION_CACHE_ENABLED",
        "GEOMETRY_DFM_RESULT_CACHE_ENABLED",
        "UPLOAD_SERVICE_URL",
    )
    saved = {f: getattr(settings, f) for f in fields}
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


# ---------------------------------------------------------------------------
# Pure helpers (no I/O)
# ---------------------------------------------------------------------------


def test_tol_bucket_for_size_buckets() -> None:
    assert tol_bucket_for(1 * 1024 * 1024) == "t002"
    assert tol_bucket_for(10 * 1024 * 1024) == "t005"
    assert tol_bucket_for(50 * 1024 * 1024) == "t010"
    assert tol_bucket_for(150 * 1024 * 1024) == "t020"


def test_tessellation_storage_path_layout() -> None:
    path = tessellation_storage_path("abc123", "t005")
    assert path == "cache/tessellation/abc123/t005.json"
    # Must live under the cache/ prefix so UploadService routes it correctly.
    assert path.startswith("cache/")


def test_dfm_result_storage_path_layout() -> None:
    path = dfm_result_storage_path("abc123", "FDM", "b1s0")
    assert path == "cache/dfm-results/abc123/FDM_b1s0.json"
    assert path.startswith("cache/")


def test_compute_flag_bucket_changes_with_flag(clean_settings) -> None:
    clean_settings.USE_BREP_THICKNESS = True
    clean_settings.USE_SDF_SMALL_FEATURES = False
    bucket_a = compute_flag_bucket()

    clean_settings.USE_SDF_SMALL_FEATURES = True
    bucket_b = compute_flag_bucket()

    assert bucket_a != bucket_b
    assert bucket_a == "b1s0"
    assert bucket_b == "b1s1"


def test_sha256_is_stable() -> None:
    assert sha256_of(b"hello") == sha256_of(b"hello")
    assert sha256_of(b"hello") != sha256_of(b"world")
    assert len(sha256_of(b"x")) == 64


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_metrics_round_trip_preserves_bytes_fields() -> None:
    metrics = {
        "volume_cm3": 12.34,
        "mesh_stl_bytes": b"\x00\x01\x02 STL DATA \x03",
        "cad_glb_bytes": b"glb_payload_bytes",
        "body_count": 2,
        "body_names": ["Body_01", "Body_02"],
    }
    blob = _serialize_metrics_dict(metrics)
    payload = json.loads(blob.decode("utf-8"))
    assert isinstance(payload["mesh_stl_bytes"], dict)
    assert "__b64__" in payload["mesh_stl_bytes"]

    restored = _deserialize_metrics_dict(blob)
    assert restored["mesh_stl_bytes"] == metrics["mesh_stl_bytes"]
    assert restored["cad_glb_bytes"] == metrics["cad_glb_bytes"]
    assert restored["volume_cm3"] == metrics["volume_cm3"]
    assert restored["body_names"] == metrics["body_names"]


def test_metrics_round_trip_when_bytes_fields_missing() -> None:
    metrics = {"volume_cm3": 1.0, "body_count": 1}
    blob = _serialize_metrics_dict(metrics)
    restored = _deserialize_metrics_dict(blob)
    assert restored == metrics


# ---------------------------------------------------------------------------
# Disabled-by-flag behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tessellation_returns_none_when_disabled(clean_settings) -> None:
    clean_settings.GEOMETRY_TESSELLATION_CACHE_ENABLED = False
    async with httpx.AsyncClient() as client:
        result = await get_cached_tessellation(
            "hash", "t002", client, _FakeTokenProvider()
        )
    assert result is None


@pytest.mark.asyncio
async def test_put_tessellation_skips_when_disabled(clean_settings) -> None:
    clean_settings.GEOMETRY_TESSELLATION_CACHE_ENABLED = False
    async with httpx.AsyncClient() as client:
        ok = await put_tessellation(
            "hash", "t002", {"k": "v"}, client, _FakeTokenProvider()
        )
    assert ok is False


@pytest.mark.asyncio
async def test_get_dfm_result_returns_none_when_disabled(clean_settings) -> None:
    clean_settings.GEOMETRY_DFM_RESULT_CACHE_ENABLED = False
    async with httpx.AsyncClient() as client:
        result = await get_cached_dfm_result(
            "hash", "FDM", "b1s0", client, _FakeTokenProvider()
        )
    assert result is None


@pytest.mark.asyncio
async def test_put_dfm_result_skips_when_disabled(clean_settings) -> None:
    clean_settings.GEOMETRY_DFM_RESULT_CACHE_ENABLED = False
    async with httpx.AsyncClient() as client:
        ok = await put_dfm_result(
            "hash", "FDM", "b1s0", {"issues": []}, client, _FakeTokenProvider()
        )
    assert ok is False


# ---------------------------------------------------------------------------
# Cache-miss / hit / failure paths via mocked UploadService
# ---------------------------------------------------------------------------

_FAKE_UPLOAD_URL = "http://upload-test.local"
_FAKE_GCS_URL = "https://storage.googleapis.com/maliev-cache/test-signed"


def _enable_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.config import settings

    monkeypatch.setattr(
        settings, "GEOMETRY_TESSELLATION_CACHE_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        settings, "GEOMETRY_DFM_RESULT_CACHE_ENABLED", True, raising=False
    )
    monkeypatch.setattr(settings, "UPLOAD_SERVICE_URL", _FAKE_UPLOAD_URL, raising=False)


@pytest.mark.asyncio
@respx.mock
async def test_get_cached_dfm_result_returns_deserialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_caches(monkeypatch)
    payload = {"issues": [{"category": "thin_wall"}], "body_count": 1}
    respx.post(f"{_FAKE_UPLOAD_URL}/upload/v1/files/by-path/signed-url").mock(
        return_value=httpx.Response(
            200,
            json={
                "signedUrl": _FAKE_GCS_URL,
                "expiresAt": "2099-01-01T00:00:00Z",
                "storagePath": "cache/dfm-results/abcdef/FDM_b1s0.json",
            },
        )
    )
    respx.get(_FAKE_GCS_URL).mock(
        return_value=httpx.Response(200, content=json.dumps(payload).encode("utf-8"))
    )
    async with httpx.AsyncClient() as client:
        result = await get_cached_dfm_result(
            "abcdef", "FDM", "b1s0", client, _FakeTokenProvider()
        )
    assert result == payload


@pytest.mark.asyncio
@respx.mock
async def test_get_cached_dfm_result_returns_none_on_410(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """410 Gone is the canonical cache-miss response from UploadService."""
    _enable_caches(monkeypatch)
    respx.post(f"{_FAKE_UPLOAD_URL}/upload/v1/files/by-path/signed-url").mock(
        return_value=httpx.Response(
            410, json={"error": "file_missing", "storagePath": "..."}
        )
    )
    async with httpx.AsyncClient() as client:
        result = await get_cached_dfm_result(
            "abcdef", "FDM", "b1s0", client, _FakeTokenProvider()
        )
    assert result is None


@pytest.mark.asyncio
@respx.mock
async def test_get_cached_dfm_result_returns_none_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404 means UploadService is not running the cache endpoint yet."""
    _enable_caches(monkeypatch)
    respx.post(f"{_FAKE_UPLOAD_URL}/upload/v1/files/by-path/signed-url").mock(
        return_value=httpx.Response(404, text="not found")
    )
    async with httpx.AsyncClient() as client:
        result = await get_cached_dfm_result(
            "abcdef", "FDM", "b1s0", client, _FakeTokenProvider()
        )
    assert result is None


@pytest.mark.asyncio
@respx.mock
async def test_get_cached_dfm_result_swallows_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_caches(monkeypatch)
    respx.post(f"{_FAKE_UPLOAD_URL}/upload/v1/files/by-path/signed-url").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    async with httpx.AsyncClient() as client:
        result = await get_cached_dfm_result(
            "abcdef", "FDM", "b1s0", client, _FakeTokenProvider()
        )
    assert result is None


@pytest.mark.asyncio
@respx.mock
async def test_put_dfm_result_writes_through_artifact_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_caches(monkeypatch)
    route = respx.post(f"{_FAKE_UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "artifactId": "abc",
                "storagePath": "cache/dfm-results/abcdef/FDM_b1s0.json",
                "downloadUrl": _FAKE_GCS_URL,
            },
        )
    )
    payload = {"issues": [], "body_count": 1}
    async with httpx.AsyncClient() as client:
        ok = await put_dfm_result(
            "abcdef", "FDM", "b1s0", payload, client, _FakeTokenProvider()
        )
    assert ok is True
    assert route.called
    request_body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert request_body["storagePath"] == "cache/dfm-results/abcdef/FDM_b1s0.json"
    assert request_body["contentType"] == "application/json"
    assert UUID(request_body["artifactId"])
    assert request_body["parentUploadId"] == "00000000-0000-0000-0000-000000000000"


@pytest.mark.asyncio
@respx.mock
async def test_put_dfm_result_swallows_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_caches(monkeypatch)
    respx.post(f"{_FAKE_UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
        return_value=httpx.Response(500, text="server error")
    )
    async with httpx.AsyncClient() as client:
        ok = await put_dfm_result(
            "abcdef", "FDM", "b1s0", {"issues": []}, client, _FakeTokenProvider()
        )
    assert ok is False


@pytest.mark.asyncio
@respx.mock
async def test_tessellation_round_trip_through_mocked_uploadservice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_caches(monkeypatch)
    metrics = {
        "volume_cm3": 1.0,
        "mesh_stl_bytes": b"BINARY_STL",
        "body_count": 1,
    }
    captured_body: dict[str, bytes] = {}

    def _on_artifact(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        # base64-decode the artifactData and stash for the GET to return.
        import base64

        captured_body["body"] = base64.b64decode(body["artifactData"])
        return httpx.Response(
            200,
            json={
                "artifactId": "abc",
                "storagePath": body["storagePath"],
                "downloadUrl": _FAKE_GCS_URL,
            },
        )

    respx.post(f"{_FAKE_UPLOAD_URL}/upload/v1/uploads/artifacts").mock(
        side_effect=_on_artifact
    )
    respx.post(f"{_FAKE_UPLOAD_URL}/upload/v1/files/by-path/signed-url").mock(
        return_value=httpx.Response(
            200,
            json={
                "signedUrl": _FAKE_GCS_URL,
                "expiresAt": "2099-01-01T00:00:00Z",
                "storagePath": "cache/tessellation/sha/t002.json",
            },
        )
    )

    def _on_gcs_get(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=captured_body.get("body", b""))

    respx.get(_FAKE_GCS_URL).mock(side_effect=_on_gcs_get)

    async with httpx.AsyncClient() as client:
        ok = await put_tessellation(
            "sha", "t002", metrics, client, _FakeTokenProvider()
        )
        assert ok is True
        artifact_request = json.loads(respx.calls[0].request.content.decode("utf-8"))
        assert artifact_request["parentUploadId"] == (
            "00000000-0000-0000-0000-000000000000"
        )
        round_tripped = await get_cached_tessellation(
            "sha", "t002", client, _FakeTokenProvider()
        )

    assert round_tripped is not None
    assert round_tripped["mesh_stl_bytes"] == metrics["mesh_stl_bytes"]
    assert round_tripped["volume_cm3"] == metrics["volume_cm3"]
