import asyncio
import logging
import os
import sys
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import APIRouter, FastAPI, responses
from fastapi.responses import JSONResponse
from scalar_fastapi import get_scalar_api_reference

from src.consumers.upload_consumer import UploadConsumer
from src.core.config import settings
from src.core.geometry import GeometryProcessor
from src.core.observability import setup_observability
from src.infrastructure.iam_registration import register_iam_permissions
from src.infrastructure.storage import HttpDownloadService

# Set up logging
# When OTEL endpoint is not configured (local dev), add stdout handler
if not settings.OTEL_EXPORTER_OTLP_ENDPOINT:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stdout,
    )

# Enable faulthandler to catch segfaults in worker processes
import faulthandler

faulthandler.enable(file=sys.stderr, all_threads=True)
logging.getLogger(__name__).info("Faulthandler enabled for crash diagnostics")

# Initialize observability as early as possible to capture startup diagnostics
setup_observability()

logger = logging.getLogger(__name__)

# Global consumer instance for cleanup
consumer: UploadConsumer | None = None


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
                "Failed to register IAM permissions - service may not have proper permissions"
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
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._timestamps: dict[str, float] = {}
        self._total_bytes = 0
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds

    def __contains__(self, key: str) -> bool:
        self._evict_expired()
        return key in self._data

    def __getitem__(self, key: str) -> dict:
        self._evict_expired()
        return self._data[key]

    def __setitem__(self, key: str, value: dict) -> None:
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
async def quality_check(upload_id: str, file_data: dict) -> JSONResponse:
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
    upload_id: str, process_code: str, request: dict | None = None, timeout: int = 30
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
        timeout: Maximum analysis time in seconds (default: 30)

    Returns:
        Process-specific DFM report with issues found for the selected manufacturing method
    """
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
    from src.infrastructure.event_publisher import publish_event

    storage_path: str | None = None
    download_url: str | None = None

    # Parse request body for cache-miss recovery
    if request:
        storage_path = request.get("storage_path")
        download_url = request.get("download_url")

    # Cache miss: need to re-download from GCS
    if upload_id not in _file_analysis_cache:
        if not storage_path and not download_url:
            return JSONResponse(
                content={
                    "upload_id": upload_id,
                    "status": "error",
                    "error_type": "NotFound",
                    "message": "Upload not found. Please provide storage_path or download_url in request body.",
                },
                status_code=404,
            )

        # Re-download from GCS
        try:
            if not download_url:
                return JSONResponse(
                    content={
                        "upload_id": upload_id,
                        "status": "error",
                        "error_type": "BadRequest",
                        "message": "download_url is required when upload_id not in cache",
                    },
                    status_code=400,
                )

            # Download file bytes from signed URL
            http_download = HttpDownloadService()
            try:
                file_stream = await http_download.download_file(download_url)
                data = file_stream.read()
                file_stream.close()

                # Run quality check to populate cache
                quality_result = _quick_quality_check(data, None, None)

                # Store file data for future use
                _file_analysis_cache[upload_id] = {
                    "stl_bytes": data,
                    "cad_bytes": None,
                    "cad_extension": None,
                }

                logger.info(
                    f"Cache-miss recovery: downloaded and cached file for {upload_id}",
                    extra={
                        "upload_id": upload_id,
                        "face_count": quality_result.get("face_count"),
                        "file_size_bytes": len(data),
                    },
                )
            finally:
                await http_download.close()

        except Exception as e:
            logger.error(
                f"Failed to re-download file for {upload_id}: {e}",
                extra={"upload_id": upload_id},
                exc_info=True,
            )
            return JSONResponse(
                content={
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
        stl_bytes = file_data["stl_bytes"]
        cad_bytes = file_data.get("cad_bytes")
        cad_extension = file_data.get("cad_extension")

        # OPTIMIZATION: Check cache for existing results
        cached = get_cached_result(stl_bytes, process_code)
        if cached is not None:
            logger.info(
                f"Cache hit for {upload_id}/{process_code} - returning cached result",
                extra={
                    "upload_id": upload_id,
                    "process_code": process_code,
                    "cache_status": "hit",
                },
            )
            return JSONResponse(
                content={
                    "upload_id": upload_id,
                    "process_code": process_code,
                    "status": "analysis_complete",
                    "dfm_report": cached,
                    "cache_status": "hit",
                }
            )

        # Run process-specific analysis with timeout
        loop = asyncio.get_event_loop()

        # Use run_in_executor to run in thread pool (prevents blocking)
        # with timeout to prevent indefinite hangs
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: _analyze_single_process(
                    stl_bytes, process_code, cad_bytes, cad_extension
                ),
            ),
            timeout=timeout,
        )

        # Check if analysis failed
        if "error_type" in result:
            logger.error(
                f"Process analysis failed for {upload_id}/{process_code}: {result.get('message')}",
                extra={
                    "upload_id": upload_id,
                    "process_code": process_code,
                    "error_type": result.get("error_type"),
                },
            )
            return JSONResponse(
                content={
                    "upload_id": upload_id,
                    "process_code": process_code,
                    "status": "error",
                    **result,
                },
                status_code=500,
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

        # OPTIMIZATION: Cache the result for future use
        cache_result(stl_bytes, process_code, result)
        logger.info(
            f"Cached result for {upload_id}/{process_code}",
            extra={
                "upload_id": upload_id,
                "process_code": process_code,
                "cache_status": "cached",
            },
        )

        # Generate overlays if GLB path is available
        overlay_paths: dict[str, str] | None = None
        if storage_path:
            try:
                # Check if we have a GLB path (use storage_path as base, replace extension with .glb)
                import os

                glb_path = f"{os.path.splitext(storage_path)[0]}.glb"

                # Build reports dict for overlay generation
                reports: dict[str, dict] = {process_code: result}

                # Generate and upload overlays (120 s hard cap so slow overlay
                # generation never blocks the HTTP response indefinitely).
                overlay_paths = await asyncio.wait_for(
                    generate_and_upload_overlays(
                        glb_path=glb_path,
                        reports=reports,
                        storage_path=storage_path,
                        storage_service=HttpDownloadService(),
                        upload_id=upload_id,
                    ),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Overlay generation timed out after 120 s for {upload_id}/{process_code}; "
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
            _now = datetime.now(timezone.utc)

            # Build the report based on process_code
            fdm_report = None
            sla_report = None
            cnc_report = None

            if process_code == "FDM":
                fdm_report = FdmDfmReport.model_validate(result)
            elif process_code == "SLA":
                sla_report = SlaDfmReport.model_validate(result)
            elif process_code in ("CNC", "CNC_MILL", "CNC_TURN"):
                cnc_report = CncDfmReport.model_validate(result)

            # Use the file_id portion of upload_id if it's a full UUID, otherwise generate one
            try:
                file_id = uuid4()
                # Try to parse upload_id as UUID first
                try:
                    from uuid import UUID

                    file_id = UUID(upload_id)
                except ValueError:
                    # upload_id is not a valid UUID, use a generated one
                    file_id = uuid4()
            except Exception:
                file_id = uuid4()

            dfm_event = DfmAnalysisReadyEvent(
                messageId=uuid4(),
                correlationId=upload_id,
                messageType=[
                    "urn:message:Maliev.MessagingContracts.Contracts.Geometry:DfmAnalysisReadyEvent"
                ],
                message=DfmAnalysisReadyMessageBody(
                    messageId=uuid4(),
                    messageName="DfmAnalysisReadyEvent",
                    messageType=MessageTypeEnum.Event,
                    messageVersion="1.0.0",
                    publishedBy="GeometryService",
                    consumedBy=["IntranetBff"],
                    correlationId=upload_id,
                    causationId=None,
                    occurredAtUtc=_now,
                    isPublic=False,
                    payload=DfmAnalysisReadyPayload(
                        fileId=str(file_id),
                        storagePath=storage_path or "",
                        fdmReport=fdm_report,
                        slaReport=sla_report,
                        cncReport=cnc_report,
                        analyzedAt=_now,
                        overlayPaths=overlay_paths,
                        bodyCount=None,  # Not tracked in on-demand endpoint
                    ),
                ),
            )
            await publish_event(dfm_event, "maliev.geometryservice.v1.dfm.ready")
            logger.info(
                f"Published DfmAnalysisReadyEvent for {upload_id}/{process_code}",
                extra={"upload_id": upload_id, "process_code": process_code},
            )
        except Exception as e:
            logger.warning(
                f"Failed to publish DfmAnalysisReadyEvent for {upload_id}/{process_code}: {e}",
                extra={"upload_id": upload_id, "process_code": process_code},
                exc_info=True,
            )

        return JSONResponse(
            content={
                "upload_id": upload_id,
                "process_code": process_code,
                "status": "analysis_complete",
                "dfm_report": result,
                "cache_status": "cold",  # First-time computation
            }
        )

    except asyncio.TimeoutError:
        logger.error(
            f"Process analysis timed out after {timeout}s for {upload_id}/{process_code}",
            extra={
                "upload_id": upload_id,
                "process_code": process_code,
                "timeout": timeout,
            },
        )
        return JSONResponse(
            content={
                "upload_id": upload_id,
                "process_code": process_code,
                "status": "timeout",
                "error_type": "TimeoutError",
                "message": f"Analysis timed out after {timeout} seconds",
            },
            status_code=504,
        )

    except Exception as e:
        logger.error(
            f"Process analysis failed for {upload_id}/{process_code}: {e}",

            extra={
                "upload_id": upload_id,
                "process_code": process_code,
                "error": str(e),
            },
            exc_info=True,
        )
        return JSONResponse(
            content={
                "upload_id": upload_id,
                "process_code": process_code,
                "status": "error",
                "error_type": type(e).__name__,
                "message": str(e),
            },
            status_code=500,
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
