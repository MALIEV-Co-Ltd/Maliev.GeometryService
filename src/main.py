import asyncio
import base64
import faulthandler
import hashlib
import logging
import os
import sys
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, TypeVar

import jwt

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # noqa: PTH118, PTH120

from fastapi import APIRouter, FastAPI, Request, responses
from fastapi.responses import JSONResponse
from scalar_fastapi import get_scalar_api_reference
from starlette.responses import Response

from src.consumers.upload_consumer import UploadConsumer
from src.core.config import settings
from src.core.event_identity import resolve_geometry_correlation_id
from src.core.geometry import GeometryProcessor
from src.core.observability import meter, setup_observability
from src.infrastructure.event_publisher import publish_event
from src.infrastructure.iam_registration import register_iam_permissions
from src.infrastructure.storage import HttpDownloadService

# Set up stdout logging so Aspire console logs remain useful even when OTLP is on.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
    stream=sys.stdout,
)

logging.getLogger("httpx").setLevel(logging.WARNING)

faulthandler.enable(file=sys.stderr, all_threads=True)
logging.getLogger(__name__).info("Faulthandler enabled for crash diagnostics")

# Initialize observability as early as possible to capture startup diagnostics
setup_observability()

logger = logging.getLogger(__name__)

# Global consumer instance for cleanup
consumer: UploadConsumer | None = None

# Phase-2 concurrency guard.  Sized from settings.GEOMETRY_DFM_SEMAPHORE so
# operators can dial down to 1 on 2 vCPU / 4 GB hosts (forces serial DFM
# requests, capping the worst-case working set at one body's footprint).
# Lazily created on first use because asyncio.Semaphore binds to the running
# loop, which doesn't exist at module import time under uvicorn.
_dfm_semaphore: asyncio.Semaphore | None = None


def _get_dfm_semaphore() -> asyncio.Semaphore:
    global _dfm_semaphore
    if _dfm_semaphore is None:
        size = max(1, int(settings.GEOMETRY_DFM_SEMAPHORE))
        _dfm_semaphore = asyncio.Semaphore(size)
    return _dfm_semaphore


def _resolve_process_dfm_timeout_seconds(timeout: float | None) -> float:
    return (
        float(settings.GEOMETRY_PROCESS_DFM_TIMEOUT_SECONDS)
        if timeout is None
        else float(timeout)
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    global consumer
    logger.info("Starting Geometry Service background consumer...")

    # Register IAM permissions via RabbitMQ
    logger.info("Registering IAM permissions...")
    try:
        registration_success = await register_iam_permissions()
        if registration_success:
            logger.info("Successfully registered IAM permissions")
        else:
            logger.warning(
                "Failed to register IAM permissions - service may not have proper permissions"  # noqa: E501
            )
    except Exception as e:
        logger.warning(f"Error registering IAM permissions: {e}")

    storage_service = HttpDownloadService()
    geometry_processor = GeometryProcessor()
    consumer = UploadConsumer(storage_service, geometry_processor)

    # Start consumer task in background
    consumer_task = asyncio.create_task(consumer.start())

    def on_task_done(t: asyncio.Task[None]) -> None:
        try:
            if not t.cancelled() and t.exception():
                logger.critical(f"Consumer task died with error: {t.exception()}")
        except asyncio.CancelledError:
            pass

    consumer_task.add_done_callback(on_task_done)

    yield

    # Shutdown
    logger.info("Shutting down Geometry Service...")
    if consumer:
        consumer_task.cancel()
        with suppress(asyncio.CancelledError):
            await consumer_task
        await consumer.stop()
        await storage_service.close()
        geometry_processor.shutdown()


app = FastAPI(
    title="MALIEV Geometry Analysis Service API",
    description=(
        "Dedicated 3D geometry analysis service for the Maliev platform. "
        "Provides automated processing of 3D mesh files (STL, OBJ, STEP) to "
        "extract critical manufacturing metrics including Volume, Surface Area, "
        "Axis-Aligned Bounding Box (AABB), and topological validity (Manifold "
        "status). Operating as an asynchronous worker, it integrates with "
        "the Upload Service and Quotation Service via RabbitMQ to provide "
        "real-time validation and cost estimation data."
    ),
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url="/geometry/openapi/v1.json",
    lifespan=lifespan,
)

# Re-run instrumentation with the app instance
setup_observability(app)

_PUBLIC_PATHS = {
    "/liveness",
    "/readiness",
    "/aspire-liveness",
    "/geometry/liveness",
    "/geometry/readiness",
    "/geometry/aspire-liveness",
    "/geometry/scalar",
    "/geometry/openapi/v1.json",
}

_CLIENT_RUNTIME_PREFIX = "/geometry/client-runtime/"
_ResponseT = TypeVar("_ResponseT", bound=Response)
_CLIENT_RUNTIME_MANIFEST_VERSION = 1
_CLIENT_RUNTIME_VERSION = "1.2.0"
_CLIENT_RUNTIME_ALGORITHM_VERSION = "browser-first-dfm-v1"
_CLIENT_RUNTIME_WORKER_BASENAME = "client-geometry-runtime.worker.js"
_CLIENT_RUNTIME_DIR = Path(__file__).resolve().parent / "client_runtime"
_CLIENT_RUNTIME_WORKER_PATH = _CLIENT_RUNTIME_DIR / _CLIENT_RUNTIME_WORKER_BASENAME
_CLIENT_RUNTIME_WASM_BYTES = base64.b64decode(
    "AGFzbQEAAAABEANgAAF/YAF/AX9gAn9/AX8DBAMAAQIHQwMPcnVudGltZV92ZXJzaW9u"
    "AAAbdHJpYW5nbGVfY291bnRfZnJvbV9pbmRpY2VzAAEPaXNfd2l0aGluX2xpbWl0AAIK"
    "FgMEAEEBCwcAIABBA24LBwAgACABTQs="
)
_CLIENT_RUNTIME_DEVICE_PROFILES = {
    "mobile": {
        "maxInputBytes": 8 * 1024 * 1024,
        "maxTriangles": 75_000,
        "timeoutMs": 8_000,
    },
    "tablet": {
        "maxInputBytes": 16 * 1024 * 1024,
        "maxTriangles": 150_000,
        "timeoutMs": 12_000,
    },
    "desktop": {
        "maxInputBytes": 32 * 1024 * 1024,
        "maxTriangles": 350_000,
        "timeoutMs": 20_000,
    },
}
_CLIENT_RUNTIME_ARTIFACT_POLICY = {
    "directBrowserViewerExtensions": [".3mf", ".glb", ".gltf", ".obj", ".stl"],
    "browserViewableUploads": {
        "viewerSource": "original_upload",
        "serverEagerMetrics": True,
        "serverGlbExport": False,
        "serverPreviewImages": False,
    },
    "serverGeneratedViewerUploads": {
        "viewerSource": "generated_glb",
        "serverEagerMetrics": True,
        "serverGlbExport": True,
        "serverPreviewImages": False,
    },
}

_CLIENT_RUNTIME_DELIVERY_COUNTER = meter.create_counter(
    "geometry.client_runtime.delivery.requests",
    description="Counts browser-first geometry runtime manifest and asset delivery.",
)
_SERVER_DFM_COUNTER = meter.create_counter(
    "geometry.server_dfm.requests",
    description="Counts GeometryService DFM requests that consume server CPU.",
)
_SERVER_DFM_INPUT_BYTES_HISTOGRAM = meter.create_histogram(
    "geometry.server_dfm.input_bytes",
    unit="By",
    description=(
        "Records input bytes for DFM workloads processed through GeometryService."
    ),
)
_SERVER_DFM_INPUT_TRIANGLES_HISTOGRAM = meter.create_histogram(
    "geometry.server_dfm.input_triangles",
    unit="{triangle}",
    description=(
        "Records triangle workloads for DFM requests processed through GeometryService."
    ),
)
_GEOMETRY_OFFLOAD_DECISION_COUNTER = meter.create_counter(
    "geometry.dfm.execution.decisions",
    description=(
        "Counts whether interactive DFM work was offloaded to the browser runtime "
        "or required GeometryService server fallback work."
    ),
)


def _add_local_runtime_headers(response: _ResponseT) -> _ResponseT:
    response.headers["X-Maliev-Geometry-Execution-Mode"] = "primary_interactive"
    response.headers["X-Maliev-Geometry-Authority"] = "local_primary"
    response.headers["X-Maliev-Geometry-Server-Role"] = "fallback_and_final_validation"
    return response


def _add_server_dfm_headers(
    response: _ResponseT,
    process_code: str,
    cache_status: str | None = None,
) -> _ResponseT:
    response.headers["X-Maliev-Geometry-Execution-Mode"] = (
        "server_fallback_final_validation"
    )
    response.headers["X-Maliev-Geometry-Authority"] = "server_authoritative"
    response.headers["X-Maliev-Geometry-Server-Role"] = "fallback_and_final_validation"
    response.headers["X-Maliev-Geometry-Process-Code"] = process_code
    if cache_status:
        response.headers["X-Maliev-Geometry-Cache-Status"] = cache_status
    return response


def _record_geometry_offload_decision(
    *,
    decision: str,
    execution_path: str,
    execution_mode: str,
    authority: str,
    server_cpu: str,
    asset_type: str | None = None,
    process_code: str | None = None,
    status_code: int | None = None,
    cache_status: str | None = None,
) -> None:
    tags: dict[str, Any] = {
        "decision": decision,
        "execution_path": execution_path,
        "execution_mode": execution_mode,
        "authority": authority,
        "server_cpu": server_cpu,
    }
    if asset_type is not None:
        tags["asset_type"] = asset_type
    if process_code is not None:
        tags["process_code"] = process_code
    if status_code is not None:
        tags["status_code"] = status_code
    if cache_status is not None:
        tags["cache_status"] = cache_status

    _GEOMETRY_OFFLOAD_DECISION_COUNTER.add(1, tags)


def _record_server_dfm_workload(
    *,
    process_code: str,
    status_code: int,
    cache_status: str,
    file_data: dict[str, Any],
) -> None:
    stl_bytes = file_data.get("stl_bytes")
    if isinstance(stl_bytes, str):
        stl_bytes = stl_bytes.encode("utf-8")

    tags: dict[str, str | int] = {
        "process_code": process_code,
        "status_code": status_code,
        "cache_status": cache_status,
        "execution_mode": "server_fallback_final_validation",
        "authority": "server_authoritative",
        "server_cpu": "consumed",
    }

    if isinstance(stl_bytes, bytes) and len(stl_bytes) > 0:
        _SERVER_DFM_INPUT_BYTES_HISTOGRAM.record(len(stl_bytes), tags)

    triangle_count = _resolve_server_dfm_triangle_count(file_data, stl_bytes)
    if triangle_count is not None and triangle_count > 0:
        _SERVER_DFM_INPUT_TRIANGLES_HISTOGRAM.record(triangle_count, tags)


def _resolve_server_dfm_triangle_count(
    file_data: dict[str, Any],
    stl_bytes: object,
) -> int | None:
    for value in (
        file_data.get("triangle_count"),
        (file_data.get("quality") or {}).get("face_count")
        if isinstance(file_data.get("quality"), dict)
        else None,
        file_data.get("face_count"),
    ):
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, float) and value > 0:
            return int(value)

    if isinstance(stl_bytes, bytes) and len(stl_bytes) >= 84:
        triangle_count = int.from_bytes(stl_bytes[80:84], "little", signed=False)
        expected_size = 84 + triangle_count * 50
        if triangle_count > 0 and expected_size == len(stl_bytes):
            return triangle_count

    return None


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_CLIENT_RUNTIME_PREFIX)


def _is_development_or_testing() -> bool:
    environment = (
        settings.ASPNETCORE_ENVIRONMENT
        or settings.ENVIRONMENT
        or os.getenv("ASPNETCORE_ENVIRONMENT", "")
        or os.getenv("DOTNET_ENVIRONMENT", "")
        or os.getenv("ENVIRONMENT", "")
    )
    return environment.lower() in {"development", "testing", "test"}


def _decode_configured_pem(configured_key: str) -> str:
    if "BEGIN" in configured_key:
        return configured_key

    try:
        return base64.b64decode(configured_key).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return configured_key


def _jwt_validation_key() -> tuple[str, str]:
    if settings.JWT_PUBLIC_KEY:
        return _decode_configured_pem(settings.JWT_PUBLIC_KEY), "RS256"

    if _is_development_or_testing() and settings.JWT_SECURITY_KEY:
        return settings.JWT_SECURITY_KEY, "HS256"

    raise RuntimeError(
        "JWT_PUBLIC_KEY is required for GeometryService HTTP authentication "
        "outside Development or Testing."
    )


def _unauthorized_response(detail: str) -> JSONResponse:
    return JSONResponse(
        content={"detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.middleware("http")
async def require_geometry_auth(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Require platform bearer tokens for non-health GeometryService routes."""
    if not settings.GEOMETRY_REQUIRE_AUTH or _is_public_path(request.url.path):
        return await call_next(request)

    authorization = request.headers.get("authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return _unauthorized_response("Authentication required")

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return _unauthorized_response("Authentication required")

    try:
        validation_key, algorithm = _jwt_validation_key()
        jwt.decode(
            token,
            validation_key,
            algorithms=[algorithm],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
        )
    except RuntimeError as exc:
        logger.error("GeometryService JWT validation is not configured: %s", exc)
        return JSONResponse(
            content={"detail": "Authentication is not configured"},
            status_code=503,
        )
    except jwt.InvalidTokenError:
        return _unauthorized_response("Invalid bearer token")

    return await call_next(request)


router = APIRouter(prefix="/geometry")


@router.get("/scalar", include_in_schema=False)
async def scalar_html() -> responses.HTMLResponse:
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
    )


@router.get("/liveness", tags=["Health"])
async def liveness() -> JSONResponse:
    """Kubernetes liveness probe."""
    return JSONResponse(content={"status": "alive"})


@router.get("/aspire-liveness", tags=["Health"])
async def aspire_liveness() -> JSONResponse:
    """Aspire liveness probe."""
    return JSONResponse(content={"status": "alive"})


@router.get("/readiness", tags=["Health"])
async def readiness() -> JSONResponse:
    """Kubernetes readiness probe."""
    return JSONResponse(content={"status": "ready"})


@router.get("/telemetry-test", tags=["Debug"])
async def telemetry_test() -> JSONResponse:
    """Endpoint to verify structured logging, metrics, and tracing."""
    logger.info(
        "Telemetry test initiated",
        extra={"test.feature": "structured-logging", "test.status": "started"},
    )

    # Example of using a metric
    from src.core.observability import meter

    counter = meter.create_counter(
        "test.telemetry.calls", description="Counts telemetry test calls"
    )
    counter.add(1, {"endpoint": "telemetry-test"})

    logger.warning("Simulated warning for telemetry verification")
    logger.error("Simulated error for telemetry verification")

    return JSONResponse(
        content={
            "message": "Telemetry data generated. Check Aspire dashboard.",
            "service": "maliev-geometryservice",
        }
    )


def _client_runtime_worker_asset_name() -> str:
    worker_bytes = _CLIENT_RUNTIME_WORKER_PATH.read_bytes()
    digest = hashlib.sha256(worker_bytes).hexdigest()[:16]
    return f"client-geometry-runtime.{digest}.worker.js"


def _client_runtime_wasm_asset_name() -> str:
    digest = hashlib.sha256(_CLIENT_RUNTIME_WASM_BYTES).hexdigest()[:16]
    return f"client-geometry-kernel.{digest}.wasm"


@router.get("/client-runtime/manifest.json", tags=["Client Runtime"])
async def client_runtime_manifest() -> JSONResponse:
    """Return the browser-first geometry runtime manifest."""
    _CLIENT_RUNTIME_DELIVERY_COUNTER.add(
        1,
        {
            "asset_type": "manifest",
            "execution_mode": "primary_interactive",
            "authority": "local_primary",
        },
    )
    _record_geometry_offload_decision(
        decision="runtime_delivered",
        execution_path="browser_primary",
        execution_mode="primary_interactive",
        authority="local_primary",
        server_cpu="avoided",
        asset_type="manifest",
    )
    worker_asset_name = _client_runtime_worker_asset_name()
    wasm_asset_name = _client_runtime_wasm_asset_name()
    response = JSONResponse(
        content={
            "manifestVersion": _CLIENT_RUNTIME_MANIFEST_VERSION,
            "runtimeVersion": _CLIENT_RUNTIME_VERSION,
            "algorithmVersion": _CLIENT_RUNTIME_ALGORITHM_VERSION,
            "runtimeKind": "browser-first-geometry",
            "executionMode": "primary_interactive",
            "authority": "local_primary",
            "isAuthoritative": False,
            "serverRole": "fallback_and_final_validation",
            "minFrontendApiVersion": 1,
            "deviceProfiles": _CLIENT_RUNTIME_DEVICE_PROFILES,
            "artifactPolicy": _CLIENT_RUNTIME_ARTIFACT_POLICY,
            "fallbackPolicy": {
                "fallbackOnUnsupportedDevice": True,
                "fallbackOnTimeout": True,
                "fallbackOnInputTooLarge": True,
                "interactiveServerDfmFallbackForBrowserPrimaryUploads": False,
                "finalValidationRequired": True,
            },
            "assets": {
                "worker": f"/geometry/client-runtime/assets/{worker_asset_name}",
                "wasm": f"/geometry/client-runtime/assets/{wasm_asset_name}",
            },
            "capabilities": {
                "inputs": {
                    "meshBuffers": True,
                    "binaryStl": True,
                    "asciiStl": True,
                    "obj": True,
                    "glb": True,
                    "gltf": True,
                    "threeMf": True,
                },
                "localOperations": [
                    "mesh_extraction",
                    "mesh_metrics",
                    "manifold_check",
                    "thin_feature_screening",
                    "process_dfm_screening",
                    "local_overlay_hints",
                    "local_preview_image_generation",
                ],
                "serverOperations": [
                    "authoritative_dfm",
                    "durable_glb_artifacts",
                    "durable_preview_images",
                    "final_quote_validation",
                ],
            },
        }
    )
    response.headers["Cache-Control"] = "no-cache"
    return _add_local_runtime_headers(response)


@router.get("/client-runtime/assets/{asset_name}", tags=["Client Runtime"])
async def client_runtime_asset(asset_name: str) -> Response:
    """Serve content-hashed browser advisory geometry runtime assets."""
    worker_asset_name = _client_runtime_worker_asset_name()
    wasm_asset_name = _client_runtime_wasm_asset_name()
    if asset_name == worker_asset_name:
        content = _CLIENT_RUNTIME_WORKER_PATH.read_bytes()
        media_type = "text/javascript; charset=utf-8"
        asset_type = "worker"
    elif asset_name == wasm_asset_name:
        content = _CLIENT_RUNTIME_WASM_BYTES
        media_type = "application/wasm"
        asset_type = "wasm"
    else:
        return JSONResponse(
            content={"detail": "Runtime asset not found"},
            status_code=404,
        )

    _CLIENT_RUNTIME_DELIVERY_COUNTER.add(
        1,
        {
            "asset_type": asset_type,
            "execution_mode": "primary_interactive",
            "authority": "local_primary",
        },
    )
    _record_geometry_offload_decision(
        decision="runtime_delivered",
        execution_path="browser_primary",
        execution_mode="primary_interactive",
        authority="local_primary",
        server_cpu="avoided",
        asset_type=asset_type,
    )
    response = Response(
        content=content,
        media_type=media_type,
    )
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return _add_local_runtime_headers(response)


# ---------------------------------------------------------------------------
# Bounded TTL cache for two-phase analysis file data.
#
# Plain dict leaks: every /quality-check call stores raw STL+CAD bytes
# keyed by upload_id and relies on a client-initiated DELETE to clean up.
# A browser close, navigation, or network failure silently leaks multi-MB
# entries that accumulate until OOM.
#
# _BoundedFileCache enforces three limits:
#   MAX_ENTRIES  — at most 20 concurrent uploads tracked
#   MAX_BYTES    — at most 500 MB total raw file bytes in memory
#   TTL_SECONDS  — entries older than 10 minutes are evicted opportunistically
#
# Eviction is eager (on every write) and O(n) on MAX_ENTRIES, which is tiny.
# ---------------------------------------------------------------------------

_MAX_CACHE_ENTRIES = 20
_MAX_CACHE_BYTES = 500 * 1024 * 1024  # 500 MB
_CACHE_TTL_SECONDS = 3600  # 1 hour (increased from 600s for lazy DFM)


class _BoundedFileCache:
    """Insertion-ordered dict with entry-count, total-byte, and TTL caps."""

    def __init__(
        self,
        max_entries: int = _MAX_CACHE_ENTRIES,
        max_bytes: int = _MAX_CACHE_BYTES,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._timestamps: dict[str, float] = {}
        self._total_bytes = 0
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds

    def __contains__(self, key: str) -> bool:
        self._evict_expired()
        return key in self._data

    def __getitem__(self, key: str) -> dict[str, Any]:
        self._evict_expired()
        return self._data[key]

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        # Compute size: stl_bytes + cad_bytes (optional)
        stl = value.get("stl_bytes") or b""
        cad = value.get("cad_bytes") or b""
        entry_bytes = len(stl) + len(cad)

        # Remove existing entry for this key before eviction checks
        if key in self._data:
            self._remove(key)

        self._evict_expired()

        # Evict oldest until within both limits
        while self._data and (
            len(self._data) >= self.max_entries
            or self._total_bytes + entry_bytes > self.max_bytes
        ):
            self._remove_oldest()

        self._data[key] = value
        self._sizes[key] = entry_bytes
        self._timestamps[key] = time.monotonic()
        self._total_bytes += entry_bytes

    def get(
        self, key: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self._evict_expired()
        return self._data.get(key, default)

    def __delitem__(self, key: str) -> None:
        if key in self._data:
            self._remove(key)
        else:
            raise KeyError(key)

    def _remove(self, key: str) -> None:
        del self._data[key]
        self._total_bytes -= self._sizes.pop(key, 0)
        self._timestamps.pop(key, None)

    def _remove_oldest(self) -> None:
        if self._data:
            oldest_key = next(iter(self._data))
            self._remove(oldest_key)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, t in self._timestamps.items() if now - t > self.ttl_seconds]
        for key in expired:
            self._remove(key)

    def __len__(self) -> int:
        return len(self._data)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes


_file_analysis_cache: _BoundedFileCache = _BoundedFileCache()


@router.post("/uploads/{upload_id}/quality-check", tags=["DFM Analysis"])
async def quality_check(upload_id: str, file_data: dict[str, Any]) -> JSONResponse:
    """Phase 1: Quick quality check on uploaded file.

    Performs fast quality checks (<5 seconds) to determine if file is valid:
    - Manifold/watertight check
    - Multi-body detection
    - Basic geometry metrics (volume, bounding box, surface area)

    Returns immediately so user can see file preview and select manufacturing process.

    Args:
        upload_id: Unique identifier for this upload
        file_data: Dictionary with keys:
            - stl_bytes: Base64-encoded STL file data (required)
            - cad_bytes: Optional base64-encoded CAD file data (STEP/IGES)
            - cad_extension: CAD file extension (e.g., "step", "stp")

    Returns:
        Quality check results with file metrics and validation status
    """
    import base64

    from src.core.geometry import _quick_quality_check

    try:
        # Decode file data
        stl_bytes = base64.b64decode(file_data.get("stl_bytes", ""))
        cad_bytes_b64 = file_data.get("cad_bytes")
        cad_bytes = base64.b64decode(cad_bytes_b64) if cad_bytes_b64 else None
        cad_extension = file_data.get("cad_extension")

        # Perform quality check
        quality_result = _quick_quality_check(stl_bytes, cad_bytes, cad_extension)

        # Store file data for Phase 2 (process-specific analysis)
        # In production, this should use proper cache with TTL
        _file_analysis_cache[upload_id] = {
            "stl_bytes": stl_bytes,
            "cad_bytes": cad_bytes,
            "cad_extension": cad_extension,
            "cad_glb_bytes": quality_result.get("cad_glb_bytes"),
            "body_count": quality_result.get("body_count", 1),
            "non_manifold_reason": quality_result.get("non_manifold_reason"),
            "non_manifold_face_count": quality_result.get("non_manifold_face_count"),
        }

        logger.info(
            f"Quality check completed for {upload_id}",
            extra={
                "upload_id": upload_id,
                "face_count": quality_result.get("face_count"),
                "complexity": quality_result.get("complexity"),
                "is_manifold": quality_result.get("is_manifold"),
            },
        )

        return JSONResponse(
            content={
                "upload_id": upload_id,
                "status": "quality_check_complete",
                "quality": quality_result,
                "ready_for_process_selection": True,
            }
        )

    except Exception as e:
        logger.error(
            f"Quality check failed for {upload_id}: {e}",
            extra={"upload_id": upload_id, "error": str(e)},
            exc_info=True,
        )
        return JSONResponse(
            content={
                "upload_id": upload_id,
                "status": "error",
                "error_type": type(e).__name__,
                "message": str(e),
            },
            status_code=500,
        )


@router.post("/uploads/{upload_id}/dfm/{process_code}", tags=["DFM Analysis"])
async def analyze_for_process(
    upload_id: str,
    process_code: str,
    request: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> JSONResponse:
    """Phase 2: Process-specific DFM analysis (on-demand, lazy DFM).

    Run DFM analysis for a SPECIFIC manufacturing process only.
    Triggered when user selects "FDM 3D Printing", "CNC Milling", etc.
    Completes in <15 seconds for typical files.

    Now supports cache-miss recovery via GCS re-download for delayed process selection.

    Args:
        upload_id: Unique identifier for this upload
        process_code: Manufacturing process code (e.g., "FDM", "SLA", "CNC_MILL", "CNC_TURN")
        request: Optional request body with {storage_path, download_url} for cache-miss recovery
        timeout: Optional analysis time override in seconds

    Returns:
        Process-specific DFM report with issues found for the selected manufacturing method
    """  # noqa: E501
    from datetime import datetime, timezone
    from uuid import uuid4

    from src.core.geometry import (
        CncDfmReport,
        FdmDfmReport,
        SlaDfmReport,
        _analyze_single_process,
        _quick_quality_check,
    )
    from src.core.geometry_optimizations import cache_result, get_cached_result
    from src.core.overlays import generate_and_upload_overlays
    from src.core.schemas import (
        DfmAnalysisReadyEvent,
        DfmAnalysisReadyMessageBody,
        DfmAnalysisReadyPayload,
        MessageTypeEnum,
    )
    from src.infrastructure.upload_cache import (
        compute_flag_bucket,
        get_cached_dfm_result,
        put_dfm_result_fire_and_forget,
        sha256_of,
    )

    storage_path: str | None = None
    download_url: str | None = None
    resolved_storage_path = ""
    analysis_timeout_seconds = _resolve_process_dfm_timeout_seconds(timeout)

    # Parse request body for cache-miss recovery
    if request:
        storage_path = request.get("storage_path")
        download_url = request.get("download_url")

    def add_process_report_defaults(report: dict[str, Any]) -> dict[str, Any]:
        if process_code in ("FDM", "SLS", "MJF", "MJ", "BJ", "DMLS"):
            report.update(
                {
                    "thinWallCount": 0,
                    "thinWallRegions": [],
                    "overhangFaceCount": 0,
                    "overhangAreaCm2": 0.0,
                    "overhangRegions": [],
                    "supportRequired": False,
                    "estimatedSupportVolumeCm3": 0.0,
                    "smallDetailCount": 0,
                }
            )
        elif process_code in ("SLA", "SLA_DLP", "DLP"):
            report.update(
                {
                    "thinWallCount": 0,
                    "thinWallRegions": [],
                    "overhangFaceCount": 0,
                    "overhangAreaCm2": 0.0,
                    "overhangRegions": [],
                    "resinTrappingRisk": False,
                    "resinTrappingRegions": [],
                    "suctionRisk": False,
                    "suctionRegions": [],
                    "hollowRegions": [],
                }
            )

        return report

    def build_timeout_report() -> dict[str, Any]:
        issue = {
            "category": "system",
            "severity": "error",
            "title": "DFM analysis timed out",
            "description": (
                f"{process_code} analysis exceeded the {analysis_timeout_seconds:g} "
                "second service limit. "
                "The model was processed, but this process-specific DFM report "
                "could not be completed."
            ),
            "value": analysis_timeout_seconds,
            "threshold": analysis_timeout_seconds,
        }

        return add_process_report_defaults(
            {
                "reportType": process_code,
                "issues": [issue],
                "analysisTimeSeconds": analysis_timeout_seconds,
            }
        )

    def build_failure_report(error_type: str, message: str) -> dict[str, Any]:
        issue = {
            "category": "system",
            "severity": "error",
            "title": "DFM analysis failed",
            "description": (
                message or f"{process_code} analysis failed before producing a report."
            ),
            "value": 0,
            "threshold": 0,
        }

        return add_process_report_defaults(
            {
                "reportType": process_code,
                "issues": [issue],
                "analysisTimeSeconds": 0.0,
                "errorType": error_type,
            }
        )

    def build_dfm_response(
        content: dict[str, Any],
        status_code: int = 200,
        cache_status: str | None = None,
    ) -> JSONResponse:
        response = JSONResponse(content=content, status_code=status_code)
        _SERVER_DFM_COUNTER.add(
            1,
            {
                "process_code": process_code,
                "status_code": status_code,
                "cache_status": cache_status or "none",
                "execution_mode": "server_fallback_final_validation",
                "authority": "server_authoritative",
            },
        )
        _record_geometry_offload_decision(
            decision="required",
            execution_path="server_fallback",
            execution_mode="server_fallback_final_validation",
            authority="server_authoritative",
            server_cpu="consumed",
            process_code=process_code,
            status_code=status_code,
            cache_status=cache_status or "none",
        )
        _record_server_dfm_workload(
            process_code=process_code,
            status_code=status_code,
            cache_status=cache_status or "none",
            file_data=_file_analysis_cache.get(upload_id, {}) or {},
        )
        return _add_server_dfm_headers(response, process_code, cache_status)

    async def publish_dfm_ready_event(
        result: dict[str, Any], overlay_paths: dict[str, str] | None = None
    ) -> None:
        _now = datetime.now(timezone.utc)
        file_data = _file_analysis_cache.get(upload_id, {}) or {}

        fdm_report = None
        sla_report = None
        cnc_report = None

        if process_code in ("FDM", "SLS", "MJF", "MJ", "BJ", "DMLS"):
            fdm_report = FdmDfmReport.model_validate(result)
        elif process_code in ("SLA", "SLA_DLP", "DLP"):
            sla_report = SlaDfmReport.model_validate(result)
        elif process_code in ("CNC", "CNC_MILL", "CNC_TURN"):
            cnc_report = CncDfmReport.model_validate(result)

        event_correlation_id = resolve_geometry_correlation_id(
            None,
            upload_id,
            resolved_storage_path,
        )

        dfm_event = DfmAnalysisReadyEvent(
            messageId=uuid4(),
            correlationId=event_correlation_id,
            messageType=[
                "urn:message:Maliev.MessagingContracts.Contracts.Geometry:DfmAnalysisReadyEvent"
            ],
            message=DfmAnalysisReadyMessageBody(
                messageId=uuid4(),
                messageName="DfmAnalysisReadyEvent",
                messageType=MessageTypeEnum.Event,
                messageVersion="1.0.0",
                publishedBy="GeometryService",
                consumedBy=["IntranetBff", "QuoteEngineBff"],
                correlationId=event_correlation_id,
                causationId=None,
                occurredAtUtc=_now,
                isPublic=False,
                payload=DfmAnalysisReadyPayload(
                    fileId=upload_id,
                    storagePath=resolved_storage_path,
                    fdmReport=fdm_report,
                    slaReport=sla_report,
                    cncReport=cnc_report,
                    analyzedAt=_now,
                    overlayPaths=overlay_paths,
                    bodyCount=file_data.get("body_count"),
                    nonManifoldReason=file_data.get("non_manifold_reason"),
                    nonManifoldFaceCount=file_data.get("non_manifold_face_count"),
                ),
            ),
        )
        await publish_event(dfm_event, "maliev.geometryservice.v1.dfm.ready")

    # Cache miss: need to re-download from GCS
    if upload_id not in _file_analysis_cache:
        if not storage_path and not download_url:
            return build_dfm_response(
                {
                    "upload_id": upload_id,
                    "status": "error",
                    "error_type": "NotFound",
                    "message": "Upload not found. Please provide storage_path or download_url in request body.",  # noqa: E501
                },
                status_code=404,
            )

        if not storage_path or not storage_path.strip():
            return build_dfm_response(
                {
                    "upload_id": upload_id,
                    "status": "invalid_state",
                    "error_type": "MissingStoragePath",
                    "message": (
                        "A canonical storage_path is required before DFM analysis."
                    ),
                },
                status_code=422,
            )

        # Re-download from GCS
        try:
            if not download_url:
                return build_dfm_response(
                    {
                        "upload_id": upload_id,
                        "status": "error",
                        "error_type": "BadRequest",
                        "message": "download_url is required when upload_id not in cache",  # noqa: E501
                    },
                    status_code=400,
                )

            # Download file bytes from signed URL
            http_download = HttpDownloadService()
            try:
                file_stream = await http_download.download_file(download_url)
                data = file_stream.read()
                file_stream.close()

                # Determine file extension from storage_path (preferred) or URL.
                from pathlib import Path as _Path
                from urllib.parse import urlparse as _urlparse

                _ext_source = storage_path or _urlparse(download_url).path
                _ext = _Path(_ext_source).suffix.lower().lstrip(".")

                if _ext in ("step", "stp", "igs", "iges"):
                    # Re-tessellate with cascadio — same path as the upload consumer.
                    # This is the only way to get real STL bytes from a CAD file.
                    from src.core.geometry import _compute_metrics_worker as _cmw

                    _loop = asyncio.get_event_loop()
                    _metrics = await _loop.run_in_executor(None, _cmw, data, _ext)
                    _stl = _metrics.get("mesh_stl_bytes") or b""
                    if not _stl:
                        return build_dfm_response(
                            {
                                "upload_id": upload_id,
                                "status": "error",
                                "error_type": "TessellationFailed",
                                "message": "Could not tessellate CAD file for DFM analysis.",  # noqa: E501
                            },
                            status_code=422,
                        )
                    _cache_entry: dict[str, Any] = {
                        "stl_bytes": _stl,
                        "stl_bytes_dict": _metrics.get("mesh_stl_bytes_dict"),
                        "cad_bytes": data,
                        "cad_extension": _ext,
                        "cad_glb_bytes": _metrics.get("cad_glb_bytes"),
                        "body_count": _metrics.get("body_count", 1),
                        "non_manifold_reason": _metrics.get("non_manifold_reason"),
                        "non_manifold_face_count": _metrics.get(
                            "non_manifold_face_count"
                        ),
                    }
                else:
                    # STL / OBJ / 3MF / etc. — bytes are already mesh data, but
                    # still need Phase 1 metrics to recover disconnected bodies.
                    from src.core.geometry import _compute_metrics_worker as _cmw

                    _loop = asyncio.get_event_loop()
                    _metrics = await _loop.run_in_executor(None, _cmw, data, _ext)
                    # Use tessellated STL bytes from metrics when available (e.g. 3MF,
                    # OBJ) so DFM analysis always gets valid STL — not raw format bytes.
                    _stl_for_cache = _metrics.get("mesh_stl_bytes") or (
                        data if _ext == "stl" else b""
                    )
                    _cache_entry = {
                        "stl_bytes": _stl_for_cache,
                        "stl_bytes_dict": _metrics.get("mesh_stl_bytes_dict"),
                        "cad_bytes": None,
                        "cad_extension": None,
                        "cad_glb_bytes": _metrics.get("cad_glb_bytes"),
                        "body_count": _metrics.get("body_count", 1),
                        "non_manifold_reason": _metrics.get("non_manifold_reason"),
                        "non_manifold_face_count": _metrics.get(
                            "non_manifold_face_count"
                        ),
                    }

                _file_analysis_cache[upload_id] = _cache_entry

                # Run quality check for logging; real work already done above.
                quality_result = _quick_quality_check(
                    _cache_entry["stl_bytes"],
                    _cache_entry.get("cad_bytes"),
                    _cache_entry.get("cad_extension"),
                )

                logger.info(
                    f"Cache-miss recovery: downloaded and cached file for {upload_id}",
                    extra={
                        "upload_id": upload_id,
                        "extension": _ext,
                        "face_count": quality_result.get("face_count"),
                        "body_count": _cache_entry.get("body_count"),
                        "file_size_bytes": len(data),
                    },
                )
            finally:
                await http_download.close()

        except Exception as e:
            from src.infrastructure.storage import PermanentDownloadError

            if isinstance(e, PermanentDownloadError):
                logger.warning(
                    f"Permanent download failure for {upload_id} — file is gone from storage: {e}",  # noqa: E501
                    extra={"upload_id": upload_id},
                )
                return build_dfm_response(
                    {
                        "upload_id": upload_id,
                        "status": "file_missing",
                        "error_type": "FileMissing",
                        "message": "The file is no longer in storage. Please re-upload.",  # noqa: E501
                    },
                    status_code=410,
                )

            logger.error(
                f"Failed to re-download file for {upload_id}: {e}",
                extra={"upload_id": upload_id},
                exc_info=True,
            )
            return build_dfm_response(
                {
                    "upload_id": upload_id,
                    "status": "error",
                    "error_type": type(e).__name__,
                    "message": f"Failed to re-download file: {str(e)}",
                },
                status_code=500,
            )

    try:
        # Retrieve stored file data
        file_data = _file_analysis_cache[upload_id]
        resolved_storage_path = str(
            storage_path or file_data.get("storage_path") or ""
        ).strip()
        if not resolved_storage_path:
            return build_dfm_response(
                {
                    "upload_id": upload_id,
                    "status": "invalid_state",
                    "error_type": "MissingStoragePath",
                    "message": (
                        "A canonical storage_path is required before DFM analysis."
                    ),
                },
                status_code=422,
            )

        stl_bytes = file_data["stl_bytes"]
        cad_bytes = file_data.get("cad_bytes")
        cad_extension = file_data.get("cad_extension")

        # OPTIMIZATION: Two-tier cache.  L1 = in-process bounded dict (fastest);
        # L2 = UploadService-backed result cache (survives restarts and is
        # shared across service replicas).  Look-up order: L1 → L2 → compute.
        cached = get_cached_result(stl_bytes, process_code)
        cache_status = "hit" if cached is not None else "cold"
        sha256_key = sha256_of(stl_bytes)
        flag_bucket = compute_flag_bucket()
        active_consumer = consumer

        if cached is None and active_consumer is not None:
            try:
                cached = await get_cached_dfm_result(
                    sha256_key,
                    process_code,
                    flag_bucket,
                    active_consumer._http_client,
                    active_consumer._token_provider,
                )
            except Exception as exc:
                logger.warning(
                    "GCS DFM cache lookup failed for %s/%s: %s",
                    upload_id,
                    process_code,
                    exc,
                    extra={"upload_id": upload_id, "process_code": process_code},
                )
                cached = None
            if cached is not None:
                cache_status = "hit_l2"
                # Backfill L1 so subsequent same-process requests skip the L2
                # round trip until the file rolls out of the bounded dict.
                cache_result(stl_bytes, process_code, cached)

        if cached is not None:
            logger.info(
                "Cache hit (%s) for %s/%s - skipping re-analysis",
                cache_status,
                upload_id,
                process_code,
                extra={
                    "upload_id": upload_id,
                    "process_code": process_code,
                    "cache_status": cache_status,
                },
            )
            result = cached
        else:
            # Run process-specific analysis with timeout, gated by the
            # Phase-2 semaphore so we don't double the working set when two
            # requests land at once on a memory-constrained host.
            loop = asyncio.get_event_loop()
            async with _get_dfm_semaphore():
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: _analyze_single_process(
                            stl_bytes, process_code, cad_bytes, cad_extension
                        ),
                    ),
                    timeout=analysis_timeout_seconds,
                )

            # Check if analysis failed
            if "error_type" in result:
                failure_report = build_failure_report(
                    str(result.get("error_type") or "AnalyzerFailed"),
                    str(result.get("message") or ""),
                )
                logger.error(
                    f"Process analysis failed for {upload_id}/{process_code}: {result.get('message')}",  # noqa: E501
                    extra={
                        "upload_id": upload_id,
                        "process_code": process_code,
                        "error_type": result.get("error_type"),
                    },
                )
                try:
                    await publish_dfm_ready_event(failure_report)
                    logger.info(
                        f"Published failure DfmAnalysisReadyEvent for {upload_id}/{process_code}",  # noqa: E501
                        extra={"upload_id": upload_id, "process_code": process_code},
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to publish failure DfmAnalysisReadyEvent for {upload_id}/{process_code}: {e}",  # noqa: E501
                        extra={"upload_id": upload_id, "process_code": process_code},
                        exc_info=True,
                    )
                return build_dfm_response(
                    {
                        "upload_id": upload_id,
                        "process_code": process_code,
                        "status": "error",
                        **result,
                        "dfm_report": failure_report,
                        "overlay_paths": {},
                        "body_count": file_data.get("body_count"),
                    },
                    status_code=500,
                    cache_status="cold",
                )

            logger.info(
                f"Process analysis completed for {upload_id}/{process_code}",
                extra={
                    "upload_id": upload_id,
                    "process_code": process_code,
                    "issues_count": len(result.get("issues", [])),
                    "analysis_time_seconds": result.get("analysis_time_seconds"),
                },
            )

            # Cache the result for future use — L1 in-process and L2 GCS.
            # The L2 write is fire-and-forget so the HTTP response is not
            # blocked by network I/O to UploadService.
            cache_result(stl_bytes, process_code, result)
            if active_consumer is not None:
                put_dfm_result_fire_and_forget(
                    sha256_key,
                    process_code,
                    flag_bucket,
                    result,
                    active_consumer._http_client,
                    active_consumer._token_provider,
                )
            logger.info(
                f"Cached result for {upload_id}/{process_code}",
                extra={
                    "upload_id": upload_id,
                    "process_code": process_code,
                    "cache_status": "cached",
                },
            )

        # Generate overlays — prefer cached STL bytes (always available in-memory)
        # over loading a GLB from a GCS path (which requires a local file).
        overlay_paths: dict[str, str] | None = None
        try:
            import os

            glb_path = f"{os.path.splitext(resolved_storage_path)[0]}.glb"  # noqa: PTH122

            # Build reports dict for overlay generation
            reports: dict[str, dict[str, Any]] = {process_code: result}

            # Pass the already-cached STL bytes so overlay generation never
            # needs to load a GLB file from disk/GCS.
            cached_stl_bytes = file_data.get("stl_bytes")
            cached_cad_glb_bytes = file_data.get("cad_glb_bytes")

            # Generate and upload overlays (120 s hard cap so slow overlay
            # generation never blocks the HTTP response indefinitely).
            if active_consumer is None:
                logger.warning("Skipping overlay upload because consumer is not ready")
            else:
                overlay_paths = await asyncio.wait_for(
                    generate_and_upload_overlays(
                        glb_path=glb_path,
                        reports=reports,
                        storage_path=resolved_storage_path,
                        upload_service_url=settings.UPLOAD_SERVICE_URL,
                        token_provider=active_consumer._token_provider,
                        http_client=active_consumer._http_client,
                        upload_id=upload_id,
                        stl_bytes=cached_stl_bytes,
                        cad_glb_bytes=cached_cad_glb_bytes,
                        cad_extension=cad_extension,
                    ),
                    timeout=120,
                )
        except asyncio.TimeoutError:
            logger.warning(
                f"Overlay generation timed out after 120 s for {upload_id}/{process_code}; "  # noqa: E501
                "returning analysis result without overlays",
                extra={"upload_id": upload_id, "process_code": process_code},
            )
        except Exception as e:
            logger.warning(
                f"Failed to generate overlays for {upload_id}/{process_code}: {e}",
                extra={"upload_id": upload_id, "process_code": process_code},
                exc_info=True,
            )

        # Publish DfmAnalysisReadyEvent
        try:
            await publish_dfm_ready_event(result, overlay_paths)
            logger.info(
                f"Published DfmAnalysisReadyEvent for {upload_id}/{process_code}",
                extra={"upload_id": upload_id, "process_code": process_code},
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                f"Failed to publish DfmAnalysisReadyEvent for {upload_id}/{process_code}: {e}",  # noqa: E501
                extra={"upload_id": upload_id, "process_code": process_code},
                exc_info=True,
            )
            return build_dfm_response(
                {
                    "upload_id": upload_id,
                    "process_code": process_code,
                    "status": "handoff_failed",
                    "error_type": "EventPublicationFailed",
                    "message": (
                        "DFM analysis completed, but the result handoff failed. "
                        "Please retry."
                    ),
                    "dfm_report": result,
                    "overlay_paths": overlay_paths or {},
                    "cache_status": cache_status,
                    "body_count": file_data.get("body_count"),
                },
                status_code=503,
                cache_status=cache_status,
            )

        return build_dfm_response(
            {
                "upload_id": upload_id,
                "process_code": process_code,
                "status": "analysis_complete",
                "dfm_report": result,
                "overlay_paths": overlay_paths or {},
                "cache_status": cache_status,
                "body_count": file_data.get("body_count"),
            },
            cache_status=cache_status,
        )

    except asyncio.TimeoutError:
        timeout_report = build_timeout_report()
        logger.error(
            f"Process analysis timed out after {analysis_timeout_seconds:g}s for {upload_id}/{process_code}",  # noqa: E501
            extra={
                "upload_id": upload_id,
                "process_code": process_code,
                "timeout": analysis_timeout_seconds,
            },
        )
        try:
            await publish_dfm_ready_event(timeout_report)
            logger.info(
                f"Published timeout DfmAnalysisReadyEvent for {upload_id}/{process_code}",  # noqa: E501
                extra={"upload_id": upload_id, "process_code": process_code},
            )
        except Exception as e:
            logger.warning(
                f"Failed to publish timeout DfmAnalysisReadyEvent for {upload_id}/{process_code}: {e}",  # noqa: E501
                extra={"upload_id": upload_id, "process_code": process_code},
                exc_info=True,
            )
        return build_dfm_response(
            {
                "upload_id": upload_id,
                "process_code": process_code,
                "status": "timeout",
                "error_type": "TimeoutError",
                "message": (
                    f"Analysis timed out after {analysis_timeout_seconds:g} seconds"
                ),
                "dfm_report": timeout_report,
                "overlay_paths": {},
                "body_count": (_file_analysis_cache.get(upload_id, {}) or {}).get(
                    "body_count"
                ),
            },
            status_code=504,
            cache_status="cold",
        )

    except Exception as e:
        failure_report = build_failure_report(type(e).__name__, str(e))
        logger.error(
            f"Process analysis failed for {upload_id}/{process_code}: {e}",
            extra={
                "upload_id": upload_id,
                "process_code": process_code,
                "error": str(e),
            },
            exc_info=True,
        )
        try:
            await publish_dfm_ready_event(failure_report)
            logger.info(
                f"Published failure DfmAnalysisReadyEvent for {upload_id}/{process_code}",  # noqa: E501
                extra={"upload_id": upload_id, "process_code": process_code},
            )
        except Exception as publish_error:
            logger.warning(
                f"Failed to publish failure DfmAnalysisReadyEvent for {upload_id}/{process_code}: {publish_error}",  # noqa: E501
                extra={"upload_id": upload_id, "process_code": process_code},
                exc_info=True,
            )
        return build_dfm_response(
            {
                "upload_id": upload_id,
                "process_code": process_code,
                "status": "error",
                "error_type": type(e).__name__,
                "message": str(e),
                "dfm_report": failure_report,
                "overlay_paths": {},
                "body_count": (_file_analysis_cache.get(upload_id, {}) or {}).get(
                    "body_count"
                ),
            },
            status_code=500,
            cache_status="cold",
        )


@router.delete("/uploads/{upload_id}", tags=["DFM Analysis"])
async def cleanup_upload(upload_id: str) -> JSONResponse:
    """Clean up cached file data for an upload.

    Should be called when user navigates away or completes the workflow.

    Args:
        upload_id: Unique identifier for the upload to clean up

    Returns:
        Confirmation of cleanup
    """
    if upload_id in _file_analysis_cache:
        del _file_analysis_cache[upload_id]
        logger.info(f"Cleaned up upload data for {upload_id}")
        return JSONResponse(content={"upload_id": upload_id, "status": "cleaned_up"})
    return JSONResponse(
        content={"upload_id": upload_id, "status": "not_found"},
        status_code=404,
    )


# Also add mirroring endpoints to the root app for extra robustness
@app.get("/liveness", include_in_schema=False)
@app.get("/readiness", include_in_schema=False)
@app.get("/aspire-liveness", include_in_schema=False)
async def root_health() -> JSONResponse:
    """Root level health checks."""
    return JSONResponse(content={"status": "healthy"})


app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    try:
        logger.info("Starting Geometry Service host")
        uvicorn.run(app, host="0.0.0.0", port=8081, log_config=None)
    except Exception as e:
        logger.critical(f"Geometry Service host terminated unexpectedly: {e}")
        sys.exit(1)
