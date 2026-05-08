"""DFM cache that goes through UploadService for GCS access.

Two cache tiers, keyed by SHA-256 of the input file bytes:

1. **Phase-1 tessellation cache** — keyed by ``(sha256, tol_bucket)``.
   Stores the full ``_compute_metrics_worker`` result dict so a re-upload
   of an identical file skips cascadio entirely.  Lifecycle: 30 days
   (enforced by the UploadService cache bucket's lifecycle rule).

2. **Phase-2 DFM result cache** — keyed by ``(sha256, process_code, flag_bucket)``.
   Stores the full DFM report dict.  Lifecycle: 7 days.

Both tiers degrade gracefully: if the corresponding ``*_ENABLED`` flag is
False, or any HTTP call fails, ``get_*`` returns ``None`` and ``put_*`` is
a no-op.  **Cache failures must never break the main analysis flow** —
every public function catches and logs exceptions rather than propagating.

GeometryService does NOT talk to GCS directly.  Reads are a two-step hop:

  1. ``POST /upload/v1/files/by-path/signed-url`` → get a 1 h signed URL
  2. ``GET <signedUrl>`` → fetch the bytes from GCS

Writes use the existing ``POST /upload/v1/uploads/artifacts`` endpoint
(same auth, same payload shape used today for GLB / thumbnail uploads),
just with a ``cache/...`` storage path that UploadService routes to the
dedicated cache bucket.

Serialization is JSON only (with base64 envelopes for raw byte fields) to
avoid executable-payload risk if a cache bucket is ever compromised.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from typing import Any, Protocol
from uuid import uuid4

import httpx

from src.core import config as _config_mod


def _settings():
    """Re-read the live ``settings`` instance on every access.

    Tests that call ``importlib.reload(src.core.config)`` (e.g.
    ``test_artifact_upload.py``) rebind the module-level ``settings`` object;
    any module that captured the old instance via ``from src.core.config
    import settings`` keeps a stale reference and silently ignores updates.
    Looking it up through the module each time keeps us in sync.
    """
    return _config_mod.settings


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path conventions and key helpers
# ---------------------------------------------------------------------------

# Both prefixes live under "cache/" so UploadService routes them to the
# dedicated cache bucket via the GcsStorageService routing rule.
_TESSELLATION_PREFIX = "cache/tessellation"
_DFM_RESULT_PREFIX = "cache/dfm-results"
_UNASSOCIATED_UPLOAD_ID = "00000000-0000-0000-0000-000000000000"


def tol_bucket_for(file_size_bytes: int) -> str:
    """Map file size onto the same buckets ``_adaptive_cascadio_tolerance`` uses.

    Returned strings (``t002``, ``t005``, ``t010``, ``t020``) embed the linear
    tolerance ×1000 so a tolerance change automatically invalidates the cache.
    Kept in sync manually with ``_adaptive_cascadio_tolerance`` in
    ``src/core/geometry.py`` — if that function changes its size→tolerance
    mapping, update both.
    """
    if file_size_bytes < 5 * 1024 * 1024:
        return "t002"
    if file_size_bytes < 25 * 1024 * 1024:
        return "t005"
    if file_size_bytes < 100 * 1024 * 1024:
        return "t010"
    return "t020"


def compute_flag_bucket() -> str:
    """Short string fingerprint of the algorithm flags that affect DFM results.

    When a flag toggles, the bucket changes and old cached results become
    unreachable — no manual eviction needed.  Keep this list in sync with the
    actual flags read by the analyzers.
    """
    parts = [
        f"b{int(_settings().USE_BREP_THICKNESS)}",
        f"s{int(_settings().USE_SDF_SMALL_FEATURES)}",
    ]
    return "".join(parts)


def tessellation_storage_path(sha256: str, tol_bucket: str) -> str:
    return f"{_TESSELLATION_PREFIX}/{sha256}/{tol_bucket}.json"


def dfm_result_storage_path(sha256: str, process_code: str, flag_bucket: str) -> str:
    return f"{_DFM_RESULT_PREFIX}/{sha256}/{process_code}_{flag_bucket}.json"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

# Top-level dict keys whose values are raw bytes and must be base64-encoded
# before JSON serialization.  Listed explicitly rather than auto-detected so
# unexpected byte fields don't get silently dropped or re-encoded.
_BYTES_FIELDS = ("mesh_stl_bytes", "cad_glb_bytes")


def _serialize_metrics_dict(metrics: dict[str, Any]) -> bytes:
    encoded: dict[str, Any] = dict(metrics)
    for field in _BYTES_FIELDS:
        value = encoded.get(field)
        if isinstance(value, bytes | bytearray):
            encoded[field] = {
                "__b64__": base64.b64encode(bytes(value)).decode("ascii"),
            }
    return json.dumps(encoded, default=str).encode("utf-8")


def _deserialize_metrics_dict(blob: bytes) -> dict[str, Any]:
    decoded = json.loads(blob.decode("utf-8"))
    for field in _BYTES_FIELDS:
        value = decoded.get(field)
        if isinstance(value, dict) and "__b64__" in value:
            decoded[field] = base64.b64decode(value["__b64__"].encode("ascii"))
    return decoded


# ---------------------------------------------------------------------------
# Token-provider protocol
# ---------------------------------------------------------------------------


class _TokenProvider(Protocol):
    """Minimal duck-type for the consumer's token provider."""

    def get_token(self) -> str: ...


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------


async def _fetch_signed_url(
    storage_path: str,
    http_client: httpx.AsyncClient,
    token_provider: _TokenProvider,
) -> str | None:
    """Ask UploadService for a signed download URL.  Returns None on miss/error."""
    upload_service_url = _settings().UPLOAD_SERVICE_URL
    try:
        token = token_provider.get_token()
        resp = await http_client.post(
            f"{upload_service_url}/upload/v1/files/by-path/signed-url",
            json={"StoragePath": storage_path, "ExpirationMinutes": 60},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_settings().GEOMETRY_CACHE_LOOKUP_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        logger.warning("cache lookup network error path=%s err=%s", storage_path, exc)
        return None
    if resp.status_code == 410:
        # Object missing — normal cache-miss path.
        logger.debug("cache miss path=%s", storage_path)
        return None
    if resp.status_code == 404:
        # Endpoint not deployed (or service down) — treat as miss, don't break flow.
        logger.debug(
            "cache lookup endpoint missing (404) path=%s — treating as miss",
            storage_path,
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "cache lookup failed status=%d path=%s body=%s",
            resp.status_code,
            storage_path,
            resp.text[:200],
        )
        return None
    try:
        body = resp.json()
    except Exception as exc:
        logger.warning(
            "cache lookup body decode failed path=%s err=%s", storage_path, exc
        )
        return None
    url = body.get("signedUrl") or body.get("SignedUrl")
    if not isinstance(url, str) or not url:
        logger.warning(
            "cache lookup returned no signedUrl path=%s body=%s",
            storage_path,
            body,
        )
        return None
    return url


async def _download_signed(
    signed_url: str, http_client: httpx.AsyncClient
) -> bytes | None:
    try:
        resp = await http_client.get(
            signed_url,
            timeout=_settings().GEOMETRY_CACHE_DOWNLOAD_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        logger.warning("cache download network error err=%s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("cache download status=%d", resp.status_code)
        return None
    return resp.content


async def _upload_via_artifact_endpoint(
    storage_path: str,
    body: bytes,
    content_type: str,
    http_client: httpx.AsyncClient,
    token_provider: _TokenProvider,
) -> bool:
    upload_service_url = _settings().UPLOAD_SERVICE_URL
    try:
        token = token_provider.get_token()
        b64_data = await asyncio.to_thread(
            lambda: base64.b64encode(body).decode("ascii")
        )
        size_mb = len(body) / (1024 * 1024)
        request_timeout = 30.0 + min(size_mb * 2, 270.0)
        resp = await http_client.post(
            f"{upload_service_url}/upload/v1/uploads/artifacts",
            json={
                "artifactId": str(uuid4()),
                "parentUploadId": _UNASSOCIATED_UPLOAD_ID,
                "storagePath": storage_path,
                "contentType": content_type,
                "artifactData": b64_data,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=request_timeout,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "cache write HTTP error status=%d path=%s",
            exc.response.status_code,
            storage_path,
        )
        return False
    except httpx.RequestError as exc:
        logger.warning("cache write network error path=%s err=%s", storage_path, exc)
        return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cache write unexpected error path=%s err=%s", storage_path, exc)
        return False


# ---------------------------------------------------------------------------
# Phase-1 tessellation cache
# ---------------------------------------------------------------------------


async def get_cached_tessellation(
    sha256: str,
    tol_bucket: str,
    http_client: httpx.AsyncClient,
    token_provider: _TokenProvider,
) -> dict[str, Any] | None:
    """Return the cached metrics-result dict, or ``None`` on miss / error."""
    if not _settings().GEOMETRY_TESSELLATION_CACHE_ENABLED:
        return None
    path = tessellation_storage_path(sha256, tol_bucket)
    signed = await _fetch_signed_url(path, http_client, token_provider)
    if signed is None:
        return None
    blob = await _download_signed(signed, http_client)
    if blob is None:
        return None
    try:
        return _deserialize_metrics_dict(blob)
    except Exception as exc:
        logger.warning("cache decode failed path=%s err=%s", path, exc)
        return None


async def put_tessellation(
    sha256: str,
    tol_bucket: str,
    metrics: dict[str, Any],
    http_client: httpx.AsyncClient,
    token_provider: _TokenProvider,
) -> bool:
    if not _settings().GEOMETRY_TESSELLATION_CACHE_ENABLED:
        return False
    path = tessellation_storage_path(sha256, tol_bucket)
    try:
        body = _serialize_metrics_dict(metrics)
    except Exception as exc:
        logger.warning("cache encode failed path=%s err=%s", path, exc)
        return False
    return await _upload_via_artifact_endpoint(
        path, body, "application/json", http_client, token_provider
    )


def put_tessellation_fire_and_forget(
    sha256: str,
    tol_bucket: str,
    metrics: dict[str, Any],
    http_client: httpx.AsyncClient,
    token_provider: _TokenProvider,
) -> None:
    """Schedule a best-effort write without awaiting completion."""
    if not _settings().GEOMETRY_TESSELLATION_CACHE_ENABLED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        put_tessellation(sha256, tol_bucket, metrics, http_client, token_provider)
    )


# ---------------------------------------------------------------------------
# Phase-2 DFM result cache
# ---------------------------------------------------------------------------


async def get_cached_dfm_result(
    sha256: str,
    process_code: str,
    flag_bucket: str,
    http_client: httpx.AsyncClient,
    token_provider: _TokenProvider,
) -> dict[str, Any] | None:
    if not _settings().GEOMETRY_DFM_RESULT_CACHE_ENABLED:
        return None
    path = dfm_result_storage_path(sha256, process_code, flag_bucket)
    signed = await _fetch_signed_url(path, http_client, token_provider)
    if signed is None:
        return None
    blob = await _download_signed(signed, http_client)
    if blob is None:
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except Exception as exc:
        logger.warning("cache decode failed path=%s err=%s", path, exc)
        return None


async def put_dfm_result(
    sha256: str,
    process_code: str,
    flag_bucket: str,
    result: dict[str, Any],
    http_client: httpx.AsyncClient,
    token_provider: _TokenProvider,
) -> bool:
    if not _settings().GEOMETRY_DFM_RESULT_CACHE_ENABLED:
        return False
    path = dfm_result_storage_path(sha256, process_code, flag_bucket)
    try:
        body = json.dumps(result, default=str).encode("utf-8")
    except Exception as exc:
        logger.warning("cache encode failed path=%s err=%s", path, exc)
        return False
    return await _upload_via_artifact_endpoint(
        path, body, "application/json", http_client, token_provider
    )


def put_dfm_result_fire_and_forget(
    sha256: str,
    process_code: str,
    flag_bucket: str,
    result: dict[str, Any],
    http_client: httpx.AsyncClient,
    token_provider: _TokenProvider,
) -> None:
    if not _settings().GEOMETRY_DFM_RESULT_CACHE_ENABLED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        put_dfm_result(
            sha256, process_code, flag_bucket, result, http_client, token_provider
        )
    )
