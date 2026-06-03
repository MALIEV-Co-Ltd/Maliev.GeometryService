import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psutil

# Fix PYTHONPATH for child processes spawned by ProcessPoolExecutor
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # noqa: PTH118, PTH120

import aio_pika
import aio_pika.abc
import httpx

from src.core.config import settings
from src.core.geometry import (
    BoundingBox,
    GeometryMetrics,
    GeometryProcessor,
    _compute_metrics_worker,
    _export_glb_from_paths,
    _render_preview_worker,
    _render_thumbnail_worker,
    compute_metrics_trimesh_only,
)
from src.core.observability import meter, tracer
from src.core.schemas import (
    BodyInfo,
    FileAnalysisFailedEvent,
    FileAnalysisFailedMessageBody,
    FileAnalysisFailedPayload,
    FileAnalyzedEvent,
    FileAnalyzedMessageBody,
    FileAnalyzedPayload,
    FileMetricsReadyEvent,
    FileMetricsReadyMessageBody,
    FileMetricsReadyPayload,
    FileUploadedEvent,
    MessageTypeEnum,
    PreviewImagesGeneratedEvent,
    PreviewImagesGeneratedMessageBody,
    PreviewImagesGeneratedPayload,
    PreviewImagesMessage,
    SmallThumbnailReadyEvent,
    SmallThumbnailReadyMessageBody,
    SmallThumbnailReadyPayload,
)
from src.infrastructure.auth import ServiceAccountTokenProvider
from src.infrastructure.event_publisher import initialize_event_publisher, publish_event
from src.infrastructure.storage import IStorageService, normalize_download_url
from src.infrastructure.upload_cache import (
    get_cached_tessellation,
    put_tessellation_fire_and_forget,
    sha256_of,
    tol_bucket_for,
)

logger = logging.getLogger(__name__)

# RSS self-defense: if the parent process crosses this threshold, force a
# gc.collect() before accepting more work.  Not a hard limit — just a nudge
# to reclaim unreferenced pages before they compound.
_RSS_GC_THRESHOLD_MB = 3_000  # 3 GB — conservative; process limit is typically 4-8 GB
_UNASSOCIATED_UPLOAD_ID = "00000000-0000-0000-0000-000000000000"


def _check_rss_and_maybe_gc(label: str) -> float:
    """Log current RSS. If above threshold, run gc.collect() and log again.

    Args:
        label: Context label for log messages (e.g. "post-phase1").

    Returns:
        Current RSS in MB after any GC.
    """
    import gc

    rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
    if rss_mb > _RSS_GC_THRESHOLD_MB:
        logger.warning(
            "RSS %.0f MB exceeds %.0f MB threshold (%s) — running gc.collect()",
            rss_mb,
            _RSS_GC_THRESHOLD_MB,
            label,
        )
        gc.collect()
        rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
        logger.info("RSS after gc.collect() [%s]: %.0f MB", label, rss_mb)
    return float(rss_mb)


# T5d: single source of truth for Phase 2 task budgets.
# All per-task timeouts are derived from this constant so log messages
# and actual deadline are always in sync.
_THUMBNAIL_BUDGET_S = 180  # single/multi-body thumbnail
_GLB_BUDGET_S = 180  # viewer GLB export
_PREVIEW_BUDGET_S = (
    60  # 7-view preview generation (reduced for faster failure detection)
)
_PHASE2_HARD_DEADLINE_S = 300
_BROWSER_VIEWER_SOURCE_EXTENSIONS = frozenset({".glb", ".obj", ".stl"})
_ARTIFACT_PIPELINE_COUNTER = meter.create_counter(
    "geometry.artifact_pipeline.operations",
    description=(
        "Counts server artifact work executed or skipped by the browser-first "
        "geometry offload pipeline."
    ),
)
_PHASE1_EXPORT_COUNTER = meter.create_counter(
    "geometry.phase1.export_requests",
    description=(
        "Counts Phase 1 GLB/STL export requests and skips for browser-first "
        "geometry offload."
    ),
)


@dataclass(frozen=True)
class ArtifactProcessingJob:
    file_id: str
    upload_id: str
    storage_path: str
    file_ext: str
    file_name: str
    file_size: int
    correlation_id: UUID | None
    metrics: GeometryMetrics
    body_count: int
    body_infos: list[BodyInfo] | None
    temp_dir: Path
    cad_glb_path: Path
    executor: Any
    queued_at: float


def _browser_viewer_source_extension(file_ext: str) -> str | None:
    """Return the direct browser viewer extension for mesh files we can load locally."""
    ext = file_ext.strip().lower()
    if not ext:
        return None
    if not ext.startswith("."):
        ext = f".{ext}"
    return ext if ext in _BROWSER_VIEWER_SOURCE_EXTENSIONS else None


def _artifact_file_extension(file_ext: str) -> str:
    ext = file_ext.strip().lower()
    if ext.startswith("."):
        ext = ext[1:]
    return ext or "unknown"


def _record_phase1_export_request(
    *,
    file_ext: str,
    artifact: str,
    status: str,
    execution_mode: str,
    reason: str,
    cache_status: str,
) -> None:
    _PHASE1_EXPORT_COUNTER.add(
        1,
        {
            "artifact": artifact,
            "status": status,
            "execution_mode": execution_mode,
            "reason": reason,
            "file_extension": _artifact_file_extension(file_ext),
            "cache_status": cache_status,
        },
    )


def _record_artifact_pipeline_operation(
    *,
    job: ArtifactProcessingJob,
    operation: str,
    status: str,
    execution_mode: str,
    reason: str,
) -> None:
    _ARTIFACT_PIPELINE_COUNTER.add(
        1,
        {
            "operation": operation,
            "status": status,
            "execution_mode": execution_mode,
            "reason": reason,
            "file_extension": _artifact_file_extension(job.file_ext),
        },
    )


def _shutdown_executor_gracefully(
    processor: "GeometryProcessor", timeout_seconds: int = 10
) -> None:
    """Gracefully shutdown a GeometryProcessor's executors with timeout.

    This function ensures that executors are properly shut down with a timeout,
    preventing orphaned processes. It tries graceful shutdown first, then falls
    back to force shutdown if needed.

    Args:
        processor: The GeometryProcessor to shutdown
        timeout_seconds: Maximum time to wait for shutdown to complete
    """

    def _do_shutdown() -> None:
        try:
            # First, try to shutdown with wait=True but with timeout
            if hasattr(processor, "executor"):
                try:
                    processor.executor.shutdown(wait=True, timeout=timeout_seconds)
                except Exception as e:
                    logger.warning(f"Executor shutdown failed: {e}")
                    # Force shutdown if graceful fails
                    with contextlib.suppress(Exception):
                        processor.executor.shutdown(wait=False)

            if hasattr(processor, "dfm_executor"):
                try:
                    processor.dfm_executor.shutdown(wait=True, timeout=timeout_seconds)
                except Exception as e:
                    logger.warning(f"DFM executor shutdown failed: {e}")
                    with contextlib.suppress(Exception):
                        processor.dfm_executor.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"Exception during shutdown: {e}")

    # Run shutdown in background thread with timeout
    shutdown_thread = threading.Thread(target=_do_shutdown, daemon=False)
    shutdown_thread.start()

    # Wait for shutdown to complete or timeout
    shutdown_thread.join(timeout=timeout_seconds + 5)

    if shutdown_thread.is_alive():
        logger.warning(
            "Shutdown thread still running after timeout - executors may leak"
        )


class UploadConsumer:
    def __init__(
        self, storage_service: IStorageService, geometry_processor: GeometryProcessor
    ) -> None:
        self._settings = settings
        self.storage_service = storage_service
        self.geometry_processor = geometry_processor
        self._token_provider = ServiceAccountTokenProvider()
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None
        self.queue: aio_pika.abc.AbstractRobustQueue | None = None
        self.exchange: aio_pika.abc.AbstractRobustExchange | None = None
        self._artifact_semaphore = asyncio.Semaphore(
            max(1, self._settings.GEOMETRY_ARTIFACT_CONCURRENCY)
        )
        self._artifact_tasks: set[asyncio.Task[None]] = set()
        # T4a: single shared client — avoids a TLS handshake per artifact upload.
        # Limits is set high enough to saturate the upload fan-out (4 Phase-2
        # tasks + N overlay GLBs).  keepalive_expiry keeps connections warm
        # between messages in a slow queue.
        self._http_client: httpx.AsyncClient = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=5.0),
        )

    def _rabbitmq_prefetch_count(self) -> int:
        configured_prefetch = self._settings.GEOMETRY_RABBITMQ_PREFETCH
        if configured_prefetch is not None and configured_prefetch > 0:
            return configured_prefetch

        return max(1, self._settings.GEOMETRY_FILE_INGEST_CONCURRENCY)

    async def connect(self) -> None:
        max_retries = 10
        base_delay = 1.0
        max_delay = 30.0

        for attempt in range(max_retries):
            try:
                self.connection = await aio_pika.connect_robust(settings.RABBITMQ_URI)
                self.channel = await self.connection.channel()
                await self.channel.set_qos(
                    prefetch_count=self._rabbitmq_prefetch_count()
                )

                self.queue = cast(
                    aio_pika.abc.AbstractRobustQueue,
                    await self.channel.declare_queue(
                        "geometry-analysis-queue", durable=True
                    ),
                )
                self.exchange = cast(
                    aio_pika.abc.AbstractRobustExchange,
                    await self.channel.declare_exchange(
                        "maliev.events", type="topic", durable=True
                    ),
                )
                # Initialize the standalone event publisher with the exchange
                initialize_event_publisher(self.exchange)
                logger.info(
                    "Successfully connected to RabbitMQ",
                    extra={
                        "event": "rabbitmq_connected",
                        "prefetch": self._rabbitmq_prefetch_count(),
                    },
                )
                return
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(
                        f"Failed to connect to RabbitMQ after "
                        f"{max_retries} attempts: {e}"
                    )
                    raise
                delay = min(base_delay * (2**attempt), max_delay)
                logger.warning(
                    f"RabbitMQ connection attempt {attempt + 1}/{max_retries} "
                    f"failed, retrying in {delay}s: {e}"
                )
                await asyncio.sleep(delay)

    async def publish_event(
        self,
        event: FileAnalyzedEvent
        | FileAnalysisFailedEvent
        | FileMetricsReadyEvent
        | PreviewImagesGeneratedEvent
        | SmallThumbnailReadyEvent,
        routing_key: str,
    ) -> None:
        """Publish event using the standalone event publisher."""
        await publish_event(event, routing_key)

    async def process_message(
        self, message: aio_pika.abc.AbstractIncomingMessage
    ) -> None:
        async with message.process():
            with tracer.start_as_current_span("process_file_upload") as span:
                file_id = "unknown"
                correlation_id = None
                try:
                    body = json.loads(message.body.decode())
                    event = FileUploadedEvent.model_validate(body)
                    correlation_id = event.correlation_id
                    inner_msg = event.message.payload

                    file_id = inner_msg.file_id or inner_msg.upload_id
                    span.set_attribute("file_id", str(file_id))

                    file_ext = Path(inner_msg.storage_path).suffix.lower()
                    supported_exts = [
                        "igs",
                        "iges",
                        "step",
                        "stp",
                        "stl",
                        "obj",
                        "3mf",
                        "gltf",
                        "glb",
                    ]

                    if file_ext.strip(".") not in supported_exts:
                        logger.debug(
                            f"Skipping file {file_id}: extension {file_ext} "
                            "not supported by geometry service"
                        )
                        return

                    logger.info(
                        "Processing 3D file",
                        extra={"file.id": str(file_id), "extension": file_ext},
                    )

                    # 1. Early file size validation (before download)
                    if not inner_msg.download_url:
                        raise ValueError("MISSING_DOWNLOAD_URL")

                    # Check Content-Length header to avoid wasting bandwidth on oversized files  # noqa: E501
                    file_size_valid = await self.validate_file_size_before_download(
                        inner_msg.download_url
                    )
                    if not file_size_valid:
                        raise ValueError("SIZE_LIMIT_EXCEEDED")

                    # 2. Download file with retry logic
                    file_stream = await self.download_with_retry(inner_msg.download_url)

                    if file_stream is None:
                        raise RuntimeError("Failed to download file")

                    try:
                        # 2. Enforce file size limit
                        file_stream.seek(0, os.SEEK_END)
                        size_mb = file_stream.tell() / (1024 * 1024)
                        if size_mb > settings.MAX_FILE_SIZE_MB:
                            raise ValueError("SIZE_LIMIT_EXCEEDED")

                        file_stream.seek(0)
                        data = file_stream.read()

                        # 3a. Phase 1 — compute metrics (fast)
                        # Add total timeout so the consumer never hangs waiting for a dead worker.  # noqa: E501
                        # The cascadio thread-based timeout in geometry.py abandons the C-extension  # noqa: E501
                        # thread after timeout_seconds; this outer timeout is a safety net.  # noqa: E501
                        PHASE1_TIMEOUT_SECONDS = (  # noqa: N806
                            300  # 5 minutes max for the whole phase
                        )
                        loop = asyncio.get_running_loop()
                        executor = self.geometry_processor.executor

                        # GCS L2 cache via UploadService: keyed on (sha256, tol_bucket).
                        # A hit lets us skip cascadio + meshing entirely and re-use the
                        # previously computed metrics dict.  Failures inside the cache
                        # module never raise — falls through to fresh compute.
                        sha256_key = sha256_of(data)
                        tol_bucket = tol_bucket_for(len(data))
                        try:
                            cached_metrics = await get_cached_tessellation(
                                sha256_key,
                                tol_bucket,
                                self._http_client,
                                self._token_provider,
                            )
                        except Exception as _cache_exc:
                            # Cache failures must never break the upload flow.
                            logger.warning(
                                f"Tessellation cache lookup failed for {file_id}: {_cache_exc}",  # noqa: E501
                                extra={"file_id": str(file_id)},
                            )
                            cached_metrics = None

                        logger.info(
                            "Starting Phase 1 metrics computation"
                            if cached_metrics is None
                            else "Phase 1 tessellation cache hit",
                            extra={
                                "event": "phase1_start"
                                if cached_metrics is None
                                else "phase1_cache_hit",
                                "file_id": str(file_id),
                                "extension": file_ext,
                                "sha256_prefix": sha256_key[:12],
                                "tol_bucket": tol_bucket,
                            },
                        )
                        try:
                            if cached_metrics is not None:
                                metrics_result = cached_metrics
                                for artifact in ("glb", "stl"):
                                    _record_phase1_export_request(
                                        file_ext=file_ext,
                                        artifact=artifact,
                                        status="cache_hit",
                                        execution_mode="server_cache",
                                        reason="tessellation_cache",
                                        cache_status="hit",
                                    )
                            else:
                                use_browser_viewer_source = (
                                    _browser_viewer_source_extension(file_ext)
                                    is not None
                                )
                                include_glb_export = not use_browser_viewer_source
                                include_stl_export = not use_browser_viewer_source
                                execution_mode = (
                                    "browser_primary"
                                    if use_browser_viewer_source
                                    else "server_primary"
                                )
                                reason = (
                                    "browser_viewer_source"
                                    if use_browser_viewer_source
                                    else "server_artifact_required"
                                )
                                _record_phase1_export_request(
                                    file_ext=file_ext,
                                    artifact="glb",
                                    status=(
                                        "requested" if include_glb_export else "skipped"
                                    ),
                                    execution_mode=execution_mode,
                                    reason=reason,
                                    cache_status="cold",
                                )
                                _record_phase1_export_request(
                                    file_ext=file_ext,
                                    artifact="stl",
                                    status=(
                                        "requested" if include_stl_export else "skipped"
                                    ),
                                    execution_mode=execution_mode,
                                    reason=reason,
                                    cache_status="cold",
                                )
                                metrics_result = await asyncio.wait_for(
                                    loop.run_in_executor(
                                        executor,
                                        _compute_metrics_worker,
                                        data,
                                        file_ext,
                                        include_glb_export,
                                        include_stl_export,
                                    ),
                                    timeout=PHASE1_TIMEOUT_SECONDS,
                                )
                            rss_mb = _check_rss_and_maybe_gc(
                                "post-phase1-cache-hit"
                                if cached_metrics is not None
                                else "post-phase1"
                            )
                            logger.info(
                                "Phase 1 metrics ready",
                                extra={
                                    "event": "phase1_complete",
                                    "file_id": str(file_id),
                                    "volume_cm3": metrics_result["volume_cm3"],
                                    "rss_mb": round(rss_mb, 1),
                                    "cache_status": "hit"
                                    if cached_metrics is not None
                                    else "cold",
                                },
                            )
                        except asyncio.TimeoutError:
                            logger.error(
                                f"Phase 1 timed out after {PHASE1_TIMEOUT_SECONDS}s — replacing executor",  # noqa: E501
                                extra={
                                    "event": "phase1_timeout",
                                    "file_id": str(file_id),
                                },
                            )
                            # Shut down the old pool in a background thread so the
                            # event loop is NOT blocked while waiting for hanging
                            # workers (e.g. cascadio C-extension threads that ignore
                            # cancellation). Sibling messages that captured the old
                            # executor reference will get BrokenProcessPool and
                            # publish their own failure events independently.
                            _old = self.geometry_processor
                            _shutdown_executor_gracefully(_old, timeout_seconds=10)
                            self.geometry_processor = GeometryProcessor()
                            raise ValueError("GEOMETRY_PROCESS_TIMEOUT") from None
                        except BrokenProcessPool as ex:
                            # ProcessPoolExecutor worker died unexpectedly (e.g., gmsh crash).  # noqa: E501
                            # Try trimesh-only fallback so we don't fail the whole job.
                            logger.warning(
                                f"Phase 1 worker crashed — trying trimesh-only fallback: {ex}",  # noqa: E501
                                extra={
                                    "event": "phase1_worker_crash",
                                    "file_id": str(file_id),
                                },
                            )
                            _old_broken = self.geometry_processor
                            _shutdown_executor_gracefully(
                                _old_broken, timeout_seconds=10
                            )
                            self.geometry_processor = GeometryProcessor()
                            executor = self.geometry_processor.executor
                            # Use trimesh directly in this process (no executor) for metrics only.  # noqa: E501
                            # This is slower but won't crash on problematic STEP files.
                            fallback_stream = io.BytesIO(data)
                            metrics_result = compute_metrics_trimesh_only(
                                fallback_stream, file_ext
                            )
                            logger.info(
                                "Phase 1 fallback complete (trimesh-only)",
                                extra={
                                    "event": "phase1_fallback_complete",
                                    "file_id": str(file_id),
                                },
                            )

                        # Write to GCS L2 cache on a cold compute (skip on hit
                        # and on the trimesh-only fallback path — that produces
                        # a less complete metrics dict we don't want to cache).
                        if cached_metrics is None and metrics_result is not None:
                            try:
                                put_tessellation_fire_and_forget(
                                    sha256_key,
                                    tol_bucket,
                                    metrics_result,
                                    self._http_client,
                                    self._token_provider,
                                )
                            except Exception as _cache_put_exc:
                                logger.warning(
                                    f"Tessellation cache write failed for {file_id}: {_cache_put_exc}",  # noqa: E501
                                    extra={"file_id": str(file_id)},
                                )

                        metrics = GeometryMetrics(
                            volumeCm3=metrics_result["volume_cm3"],
                            supportVolumeCm3=metrics_result["support_volume_cm3"],
                            surfaceAreaCm2=metrics_result["surface_area_cm2"],
                            boundingBox=BoundingBox(**metrics_result["bounding_box"]),
                            isManifold=metrics_result["is_manifold"],
                            triangleCount=metrics_result["triangle_count"],
                            eulerNumber=metrics_result["euler_number"],
                            nonManifoldReason=metrics_result.get("non_manifold_reason"),
                            nonManifoldFaceCount=metrics_result.get(
                                "non_manifold_face_count"
                            ),
                        )

                        # Extract body metadata
                        body_count = metrics_result.get("body_count", 1)
                        body_names = metrics_result.get("body_names", [])
                        body_volumes = metrics_result.get("body_volumes_cm3", [])
                        body_infos: list[BodyInfo] | None = None
                        if body_count > 1:
                            names = (
                                body_names
                                if len(body_names) == body_count
                                else [f"Body {i + 1}" for i in range(body_count)]
                            )
                            body_infos = [
                                BodyInfo(
                                    index=i,
                                    name=name,
                                    volumeCm3=body_volumes[i]
                                    if i < len(body_volumes)
                                    else None,
                                    bboxMin=None,
                                    bboxMax=None,
                                )
                                for i, name in enumerate(names)
                            ]

                        # Publish early metrics event
                        _metrics_now = datetime.now(timezone.utc)
                        metrics_event = FileMetricsReadyEvent(
                            messageId=uuid4(),
                            correlationId=correlation_id,
                            messageType=[
                                "urn:message:Maliev.MessagingContracts.Contracts.Geometry:FileMetricsReadyEvent"
                            ],
                            message=FileMetricsReadyMessageBody(
                                messageId=uuid4(),
                                messageName="FileMetricsReadyEvent",
                                messageType=MessageTypeEnum.Event,
                                messageVersion="1.0.0",
                                publishedBy="GeometryService",
                                consumedBy=["IntranetBff"],
                                correlationId=correlation_id,
                                causationId=None,
                                occurredAtUtc=_metrics_now,
                                isPublic=False,
                                payload=FileMetricsReadyPayload(
                                    fileId=file_id,
                                    storagePath=inner_msg.storage_path,
                                    metrics=metrics,
                                    processedAt=_metrics_now,
                                    bodyCount=body_count,
                                    bodies=body_infos,
                                ),
                            ),
                        )
                        await self.publish_event(
                            metrics_event,
                            "maliev.geometryservice.v1.metrics.ready",
                        )

                        await self._schedule_artifact_job_from_metrics(
                            metrics_result=metrics_result,
                            metrics=metrics,
                            file_id=str(file_id),
                            upload_id=inner_msg.upload_id,
                            storage_path=inner_msg.storage_path,
                            file_ext=file_ext,
                            file_name=inner_msg.file_name,
                            file_size=inner_msg.file_size,
                            correlation_id=correlation_id,
                            body_count=body_count,
                            body_infos=body_infos,
                        )
                        logger.info(
                            "Upload message acknowledged after metrics stage; "
                            "artifact processing continues asynchronously",
                            extra={
                                "event": "upload_ingest_complete",
                                "file_id": str(file_id),
                                "body_count": body_count,
                            },
                        )
                        return

                    finally:
                        file_stream.close()
                        del file_stream

                except ValueError as e:
                    error_code = str(e)
                    if "MULTI_BODY_ERROR" in error_code:
                        error_code = "MULTI_BODY_ERROR"
                    elif "SIZE_LIMIT_EXCEEDED" in error_code:
                        error_code = "SIZE_LIMIT_EXCEEDED"
                    elif "GEOMETRY_PROCESS_TIMEOUT" in error_code:
                        error_code = "GEOMETRY_PROCESS_TIMEOUT"
                    elif "MISSING_DOWNLOAD_URL" in error_code:
                        error_code = "MISSING_DOWNLOAD_URL"
                    else:
                        error_code = "FILE_CORRUPT"

                    await self.publish_failure(
                        correlation_id,
                        file_id,
                        error_code,
                        str(e),
                        inner_msg.storage_path,
                    )
                except Exception as e:
                    logger.error(f"Error processing {file_id}: {e}")
                    await self.publish_failure(
                        correlation_id,
                        file_id,
                        "SYSTEM_ERROR",
                        str(e),
                        inner_msg.storage_path,
                    )

    async def _schedule_artifact_job_from_metrics(
        self,
        *,
        metrics_result: dict[str, Any],
        metrics: GeometryMetrics,
        file_id: str,
        upload_id: str,
        storage_path: str,
        file_ext: str,
        file_name: str,
        file_size: int,
        correlation_id: UUID | None,
        body_count: int,
        body_infos: list[BodyInfo] | None,
    ) -> None:
        cad_glb_bytes = metrics_result.get("cad_glb_bytes")
        original_stl_bytes = metrics_result.get("mesh_stl_bytes")
        logger.info(
            "Phase 2 artifact job inputs prepared",
            extra={
                "event": "phase2_inputs",
                "file_id": file_id,
                "cad_glb_bytes": len(cad_glb_bytes) if cad_glb_bytes else 0,
                "mesh_stl_bytes": len(original_stl_bytes) if original_stl_bytes else 0,
                "body_count": body_count,
            },
        )

        if not cad_glb_bytes:
            if _browser_viewer_source_extension(file_ext) is not None:
                temp_dir = Path(tempfile.mkdtemp(prefix="geom_"))
                job = ArtifactProcessingJob(
                    file_id=file_id,
                    upload_id=upload_id,
                    storage_path=storage_path,
                    file_ext=file_ext,
                    file_name=file_name,
                    file_size=file_size,
                    correlation_id=correlation_id,
                    metrics=metrics,
                    body_count=body_count,
                    body_infos=body_infos,
                    temp_dir=temp_dir,
                    cad_glb_path=temp_dir / "cad.glb",
                    executor=self.geometry_processor.executor,
                    queued_at=time.perf_counter(),
                )
                task = asyncio.create_task(self._run_artifact_job(job))
                self._track_artifact_task(task, job)
                logger.info(
                    "Phase 2 browser viewer source job scheduled "
                    "without server GLB bytes",
                    extra={
                        "event": "phase2_browser_source_no_glb_scheduled",
                        "file_id": file_id,
                        "active_artifact_tasks": len(self._artifact_tasks),
                    },
                )
                return

            logger.warning(
                "No GLB bytes available from Phase 1; artifact job not scheduled",
                extra={"event": "phase2_skip", "file_id": file_id},
            )
            await self.publish_failure(
                correlation_id,
                file_id,
                "GEOMETRY_NO_RESULT",
                "Phase 1 produced no GLB bytes",
                storage_path,
            )
            return

        temp_dir = Path(tempfile.mkdtemp(prefix="geom_"))
        cad_glb_path = temp_dir / "cad.glb"
        try:
            cad_glb_path.write_bytes(cad_glb_bytes)
        except Exception:
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        job = ArtifactProcessingJob(
            file_id=file_id,
            upload_id=upload_id,
            storage_path=storage_path,
            file_ext=file_ext,
            file_name=file_name,
            file_size=file_size,
            correlation_id=correlation_id,
            metrics=metrics,
            body_count=body_count,
            body_infos=body_infos,
            temp_dir=temp_dir,
            cad_glb_path=cad_glb_path,
            executor=self.geometry_processor.executor,
            queued_at=time.perf_counter(),
        )
        task = asyncio.create_task(self._run_artifact_job(job))
        self._track_artifact_task(task, job)
        logger.info(
            "Phase 2 artifact job scheduled",
            extra={
                "event": "phase2_scheduled",
                "file_id": file_id,
                "active_artifact_tasks": len(self._artifact_tasks),
            },
        )

    def _track_artifact_task(
        self, task: asyncio.Task[None], job: ArtifactProcessingJob
    ) -> None:
        self._artifact_tasks.add(task)

        def _on_done(done_task: asyncio.Task[None]) -> None:
            self._artifact_tasks.discard(done_task)
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc:
                    logger.error(
                        "Artifact job failed",
                        exc_info=(type(exc), exc, exc.__traceback__),
                        extra={
                            "event": "phase2_task_unhandled_error",
                            "file_id": job.file_id,
                        },
                    )

        task.add_done_callback(_on_done)

    async def wait_for_artifact_tasks(self) -> None:
        while self._artifact_tasks:
            await asyncio.gather(*list(self._artifact_tasks), return_exceptions=True)

    async def stop(self) -> None:
        for task in list(self._artifact_tasks):
            task.cancel()
        await self.wait_for_artifact_tasks()
        await self._http_client.aclose()

    async def _run_artifact_job(self, job: ArtifactProcessingJob) -> None:
        async with self._artifact_semaphore:
            queue_wait_ms = round((time.perf_counter() - job.queued_at) * 1000, 1)
            started_at = time.perf_counter()
            with tracer.start_as_current_span("process_file_artifacts") as span:
                span.set_attribute("file_id", job.file_id)
                span.set_attribute("artifact.queue_wait_ms", queue_wait_ms)
                logger.info(
                    "Phase 2 artifact job started",
                    extra={
                        "event": "phase2_start",
                        "file_id": job.file_id,
                        "queue_wait_ms": queue_wait_ms,
                    },
                )
                try:
                    viewer_source_published = await self._publish_browser_viewer_source(
                        job
                    )
                    if viewer_source_published:
                        span.set_attribute("artifact.viewer_source", "browser")
                        span.set_attribute("artifact.secondary_artifacts_skipped", True)
                        _record_artifact_pipeline_operation(
                            job=job,
                            operation="secondary_artifacts",
                            status="skipped",
                            execution_mode="browser_primary",
                            reason="browser_viewer_source",
                        )
                        logger.info(
                            "Skipping server thumbnail and preview generation "
                            "for browser-renderable upload",
                            extra={
                                "event": "browser_viewer_secondary_artifacts_skipped",
                                "file_id": job.file_id,
                                "storage_path": job.storage_path,
                                "file_ext": job.file_ext,
                            },
                        )
                        return

                    if not viewer_source_published:
                        _record_artifact_pipeline_operation(
                            job=job,
                            operation="viewer_glb_export",
                            status="scheduled",
                            execution_mode="server_artifact",
                            reason="browser_source_unavailable",
                        )
                        glb_published = await self._publish_glb(job)
                        if not glb_published:
                            _record_artifact_pipeline_operation(
                                job=job,
                                operation="viewer_glb_export",
                                status="failed",
                                execution_mode="server_artifact",
                                reason="browser_source_unavailable",
                            )
                            await self.publish_failure(
                                job.correlation_id,
                                job.file_id,
                                "GEOMETRY_NO_RESULT",
                                "Phase 2 did not produce a browser viewer source",
                                job.storage_path,
                            )
                            return

                    _record_artifact_pipeline_operation(
                        job=job,
                        operation="secondary_artifacts",
                        status="scheduled",
                        execution_mode="server_artifact",
                        reason="server_viewer_artifact",
                    )
                    secondary_timeout_s = max(
                        0.0,
                        _PHASE2_HARD_DEADLINE_S - (time.perf_counter() - started_at),
                    )
                    tasks = {
                        "thumb": asyncio.create_task(
                            self._publish_small_thumbnail(job)
                        ),
                        "previews": asyncio.create_task(self._publish_previews(job)),
                    }
                    done, pending = await asyncio.wait(
                        tasks.values(),
                        timeout=secondary_timeout_s,
                    )
                    for pending_task in pending:
                        pending_task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                        logger.warning(
                            "Phase 2 artifact tasks cancelled after deadline",
                            extra={
                                "event": "phase2_deadline",
                                "file_id": job.file_id,
                                "pending_count": len(pending),
                            },
                        )

                    for name, task in tasks.items():
                        if task not in done:
                            continue
                        try:
                            task.result()
                        except Exception as exc:
                            logger.warning(
                                "Phase 2 artifact task failed",
                                extra={
                                    "event": "phase2_artifact_error",
                                    "file_id": job.file_id,
                                    "artifact": name,
                                    "error": str(exc),
                                },
                            )

                finally:
                    import shutil

                    shutil.rmtree(job.temp_dir, ignore_errors=True)
                    duration_ms = round((time.perf_counter() - started_at) * 1000, 1)
                    _check_rss_and_maybe_gc("post-phase2")
                    logger.info(
                        "Phase 2 artifact job finished",
                        extra={
                            "event": "phase2_complete",
                            "file_id": job.file_id,
                            "duration_ms": duration_ms,
                        },
                    )

    async def _publish_small_thumbnail(self, job: ArtifactProcessingJob) -> bool:
        timeout_s = 180 if job.body_count > 1 else 60
        started_at = time.perf_counter()
        try:
            loop = asyncio.get_running_loop()
            thumb = await asyncio.wait_for(
                loop.run_in_executor(
                    job.executor,
                    _render_thumbnail_worker,
                    str(job.cad_glb_path),
                ),
                timeout=timeout_s,
            )
            if not thumb:
                return False
            thumb_path = f"{job.storage_path}_thumbnail_small.webp"
            uploaded = await self.upload_artifact(
                thumb,
                thumb_path,
                "image/webp",
                job.upload_id,
            )
            if not uploaded:
                return False
            now = datetime.now(timezone.utc)
            event = SmallThumbnailReadyEvent(
                messageId=uuid4(),
                correlationId=job.correlation_id,
                messageType=[
                    "urn:message:Maliev.MessagingContracts.Contracts.Geometry:SmallThumbnailReadyEvent"
                ],
                message=SmallThumbnailReadyMessageBody(
                    messageId=uuid4(),
                    messageName="SmallThumbnailReadyEvent",
                    messageType=MessageTypeEnum.Event,
                    messageVersion="1.0.0",
                    publishedBy="GeometryService",
                    consumedBy=["IntranetBff"],
                    correlationId=job.correlation_id,
                    causationId=None,
                    occurredAtUtc=now,
                    isPublic=False,
                    payload=SmallThumbnailReadyPayload(
                        fileId=job.file_id,
                        storagePath=job.storage_path,
                        thumbnailStoragePath=thumb_path,
                    ),
                ),
            )
            await self.publish_event(
                event,
                "maliev.geometryservice.v1.thumbnail.small.ready",
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Small thumbnail timed out",
                extra={
                    "event": "thumbnail_small_timeout",
                    "file_id": job.file_id,
                    "timeout_s": timeout_s,
                },
            )
            return False
        except Exception as exc:
            logger.warning(
                "Small thumbnail task failed",
                extra={
                    "event": "thumbnail_small_error",
                    "file_id": job.file_id,
                    "error": str(exc),
                },
            )
            return False
        finally:
            logger.info(
                "Small thumbnail artifact stage finished",
                extra={
                    "event": "thumbnail_small_finished",
                    "file_id": job.file_id,
                    "duration_ms": round(
                        (time.perf_counter() - started_at) * 1000,
                        1,
                    ),
                },
            )

    async def _publish_glb(self, job: ArtifactProcessingJob) -> bool:
        started_at = time.perf_counter()
        try:
            loop = asyncio.get_running_loop()
            glb = await asyncio.wait_for(
                loop.run_in_executor(
                    job.executor,
                    _export_glb_from_paths,
                    str(job.cad_glb_path),
                    job.file_ext,
                ),
                timeout=_GLB_BUDGET_S,
            )
            if not glb:
                return False
            glb_path = f"{job.storage_path}_viewer.glb"
            uploaded = await self.upload_artifact(
                glb,
                glb_path,
                "model/gltf-binary",
                job.upload_id,
            )
            if not uploaded:
                return False
            await self._publish_file_analyzed_event(job, glb_path)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "GLB export timed out",
                extra={
                    "event": "glb_timeout",
                    "file_id": job.file_id,
                    "timeout_s": _GLB_BUDGET_S,
                },
            )
            return await self._publish_source_glb_fallback(job, "timeout")
        except Exception as exc:
            logger.warning(
                "GLB artifact task failed",
                extra={
                    "event": "glb_error",
                    "file_id": job.file_id,
                    "error": str(exc),
                },
            )
            return False
        finally:
            logger.info(
                "GLB artifact stage finished",
                extra={
                    "event": "glb_finished",
                    "file_id": job.file_id,
                    "duration_ms": round(
                        (time.perf_counter() - started_at) * 1000,
                        1,
                    ),
                },
            )

    async def _publish_browser_viewer_source(self, job: ArtifactProcessingJob) -> bool:
        viewer_file_extension = _browser_viewer_source_extension(job.file_ext)
        if viewer_file_extension is None:
            return False

        await self._publish_file_analyzed_event(
            job,
            glb_path=None,
            viewer_storage_path=job.storage_path,
            viewer_file_extension=viewer_file_extension,
        )
        _record_artifact_pipeline_operation(
            job=job,
            operation="viewer_source",
            status="published",
            execution_mode="browser_primary",
            reason="direct_source",
        )
        logger.info(
            "Published original upload as browser viewer source",
            extra={
                "event": "browser_viewer_source_published",
                "file_id": job.file_id,
                "storage_path": job.storage_path,
                "viewer_file_extension": viewer_file_extension,
            },
        )
        return True

    async def _publish_source_glb_fallback(
        self, job: ArtifactProcessingJob, reason: str
    ) -> bool:
        try:
            source_glb = await asyncio.to_thread(job.cad_glb_path.read_bytes)
        except Exception as exc:
            logger.warning(
                "GLB fallback source read failed",
                extra={
                    "event": "glb_fallback_read_error",
                    "file_id": job.file_id,
                    "reason": reason,
                    "error": str(exc),
                },
            )
            return False

        glb_path = f"{job.storage_path}_viewer.glb"
        uploaded = await self.upload_artifact(
            source_glb,
            glb_path,
            "model/gltf-binary",
            job.upload_id,
        )
        if not uploaded:
            return False

        await self._publish_file_analyzed_event(job, glb_path)
        logger.warning(
            "Published source GLB fallback after viewer export failed",
            extra={
                "event": "glb_fallback_published",
                "file_id": job.file_id,
                "reason": reason,
                "bytes": len(source_glb),
            },
        )
        return True

    async def _publish_file_analyzed_event(
        self,
        job: ArtifactProcessingJob,
        glb_path: str | None,
        viewer_storage_path: str | None = None,
        viewer_file_extension: str | None = None,
    ) -> None:
        resolved_viewer_storage_path = viewer_storage_path or glb_path
        resolved_viewer_file_extension = viewer_file_extension
        if resolved_viewer_storage_path and not resolved_viewer_file_extension:
            resolved_viewer_file_extension = ".glb"

        now = datetime.now(timezone.utc)
        event = FileAnalyzedEvent(
            messageId=uuid4(),
            correlationId=job.correlation_id,
            messageType=[
                "urn:message:Maliev.MessagingContracts.Contracts.Geometry:FileAnalyzedEvent"
            ],
            message=FileAnalyzedMessageBody(
                messageId=uuid4(),
                messageName="FileAnalyzedEvent",
                messageType=MessageTypeEnum.Event,
                messageVersion="1.0.0",
                publishedBy="GeometryService",
                consumedBy=["IntranetBff"],
                correlationId=job.correlation_id,
                causationId=None,
                occurredAtUtc=now,
                isPublic=False,
                payload=FileAnalyzedPayload(
                    fileId=job.file_id,
                    metrics=job.metrics,
                    processedAt=now,
                    glbStoragePath=glb_path,
                    viewerStoragePath=resolved_viewer_storage_path,
                    viewerFileExtension=resolved_viewer_file_extension,
                    thumbnailStoragePath=None,
                    storagePath=job.storage_path,
                    dfmReport=None,
                    bodyCount=job.body_count,
                    bodies=job.body_infos,
                ),
            ),
        )
        await self.publish_event(
            event,
            "maliev.geometryservice.v1.analysis.completed",
        )

    async def _publish_previews(self, job: ArtifactProcessingJob) -> bool:
        started_at = time.perf_counter()
        try:
            loop = asyncio.get_running_loop()
            preview_images = await asyncio.wait_for(
                loop.run_in_executor(
                    job.executor,
                    _render_preview_worker,
                    str(job.cad_glb_path),
                ),
                timeout=_PREVIEW_BUDGET_S,
            )

            preview_paths: dict[str, str] = {}
            thumbnail_small_path: str | None = None
            thumbnail_large_path: str | None = None
            for side, image_bytes in preview_images.items():
                if side in ("thumbnail_small", "thumbnail_large"):
                    continue
                if image_bytes:
                    preview_path = f"{job.storage_path}_preview_{side}.webp"
                    ok = await self.upload_artifact(
                        image_bytes,
                        preview_path,
                        "image/webp",
                        job.upload_id,
                    )
                    if ok:
                        preview_paths[side] = preview_path

            thumbnail_small_bytes = preview_images.get("thumbnail_small")
            if thumbnail_small_bytes:
                thumbnail_small_path = f"{job.storage_path}_thumbnail_small.webp"
                small_ok = await self.upload_artifact(
                    thumbnail_small_bytes,
                    thumbnail_small_path,
                    "image/webp",
                    job.upload_id,
                )
                if not small_ok:
                    thumbnail_small_path = None

            thumbnail_large_bytes = preview_images.get("thumbnail_large")
            if thumbnail_large_bytes:
                thumbnail_large_path = f"{job.storage_path}_thumbnail_large.webp"
                large_ok = await self.upload_artifact(
                    thumbnail_large_bytes,
                    thumbnail_large_path,
                    "image/webp",
                    job.upload_id,
                )
                if not large_ok:
                    thumbnail_large_path = None

            now = datetime.now(timezone.utc)
            event = PreviewImagesGeneratedEvent(
                messageId=uuid4(),
                correlationId=job.correlation_id,
                messageType=[
                    "urn:message:Maliev.MessagingContracts.Contracts.Geometry:PreviewImagesGeneratedEvent"
                ],
                message=PreviewImagesGeneratedMessageBody(
                    messageId=uuid4(),
                    messageName="PreviewImagesGeneratedEvent",
                    messageType=MessageTypeEnum.Event,
                    messageVersion="1.0.0",
                    publishedBy="GeometryService",
                    consumedBy=["IntranetBff"],
                    correlationId=job.correlation_id,
                    causationId=None,
                    occurredAtUtc=now,
                    isPublic=False,
                    payload=PreviewImagesGeneratedPayload(
                        fileId=job.file_id,
                        storagePath=job.storage_path,
                        previewImages=PreviewImagesMessage(
                            frontSmall=preview_paths.get("front_small"),
                            backSmall=preview_paths.get("back_small"),
                            leftSmall=preview_paths.get("left_small"),
                            rightSmall=preview_paths.get("right_small"),
                            topSmall=preview_paths.get("top_small"),
                            bottomSmall=preview_paths.get("bottom_small"),
                            thumbnailSmall=thumbnail_small_path,
                            thumbnailLarge=thumbnail_large_path,
                        ),
                        generatedAt=now,
                    ),
                ),
            )
            await self.publish_event(
                event,
                "maliev.geometryservice.v1.preview-images.generated",
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Previews timed out",
                extra={
                    "event": "previews_timeout",
                    "file_id": job.file_id,
                    "timeout_s": _PREVIEW_BUDGET_S,
                },
            )
            return False
        except Exception as exc:
            logger.warning(
                "Previews artifact task failed",
                extra={
                    "event": "previews_error",
                    "file_id": job.file_id,
                    "error": str(exc),
                },
            )
            return False
        finally:
            logger.info(
                "Previews artifact stage finished",
                extra={
                    "event": "previews_finished",
                    "file_id": job.file_id,
                    "duration_ms": round(
                        (time.perf_counter() - started_at) * 1000,
                        1,
                    ),
                },
            )

    async def validate_file_size_before_download(self, url: str) -> bool:
        """Check Content-Length header before downloading to reject oversized files early.

        Returns True if file size is within limits, False if exceeds MAX_FILE_SIZE_MB.
        """  # noqa: E501
        try:
            # T4a: reuse the shared client instead of creating a fresh one.
            response = await self._http_client.head(normalize_download_url(url))
            content_length = response.headers.get("Content-Length")
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                if size_mb > settings.MAX_FILE_SIZE_MB:
                    logger.warning(
                        f"File exceeds size limit: {size_mb:.1f}MB > {settings.MAX_FILE_SIZE_MB}MB - rejecting early",  # noqa: E501
                        extra={
                            "event": "file_size_early_rejection",
                            "size_mb": size_mb,
                        },
                    )
                    return False
                logger.info(
                    f"File size check passed: {size_mb:.1f}MB",
                    extra={"event": "file_size_validated", "size_mb": size_mb},
                )
                return True
            # Content-Length not available, proceed with download
            logger.debug(
                "Content-Length header not available, skipping early size check"
            )
            return True
        except Exception as e:
            logger.warning(
                f"Failed to check file size before download: {e}, proceeding anyway"
            )
            return True  # Proceed with download if HEAD request fails

    async def download_with_retry(self, url: str, attempts: int = 3) -> io.BytesIO:
        """Implements 3-attempt retry logic with exponential backoff."""
        from src.infrastructure.storage import PermanentDownloadError

        for i in range(attempts):
            try:
                return await self.storage_service.download_file(url)
            except PermanentDownloadError:
                raise
            except Exception as e:
                if i == attempts - 1:
                    raise e
                wait_time = 2 ** (i + 1)
                logger.warning(
                    f"Download failed, retrying in {wait_time}s... "
                    f"(Attempt {i + 1}/{attempts})"
                )
                await asyncio.sleep(wait_time)

        raise RuntimeError("Unexpected: download_with_retry exhausted all attempts")

    async def upload_artifact(
        self,
        data: bytes,
        path: str,
        content_type: str,
        parent_upload_id: str = _UNASSOCIATED_UPLOAD_ID,
    ) -> bool:
        """Uploads an artifact (GLB/PNG) to GCS via UploadService HTTP endpoint.

        Returns True if the upload succeeded, False otherwise.
        Callers that intend to publish a signed-URL event MUST check the return value
        and skip publishing when False — otherwise the frontend receives a URL that
        resolves to a non-existent GCS object (404).
        """
        try:
            upload_service_url = settings.UPLOAD_SERVICE_URL
            artifact_id = str(uuid4())
            token = self._token_provider.get_token()

            # T4b: move base64 encoding off the event loop — large blobs (e.g. a
            # 100 MB GLB) expand to ~133 MB JSON and block all coroutines for
            # hundreds of milliseconds when encoded synchronously.
            b64_data = await asyncio.to_thread(
                lambda: base64.b64encode(data).decode("utf-8")
            )

            payload = {
                "artifactId": artifact_id,
                "parentUploadId": parent_upload_id,
                "storagePath": path,
                "contentType": content_type,
                "artifactData": b64_data,
            }

            # T4a: reuse the shared client — adaptive per-request timeout via
            # the extensions dict (overrides the client-level timeout for this call).
            size_mb = len(data) / (1024 * 1024)
            request_timeout = 30.0 + min(size_mb * 2, 270.0)  # 30s–300s
            response = await self._http_client.post(
                f"{upload_service_url}/upload/v1/uploads/artifacts",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=request_timeout,
            )
            response.raise_for_status()
            result = response.json()
            logger.info(f"Artifact uploaded successfully: {result.get('storagePath')}")
            return True

        except httpx.HTTPStatusError as e:
            logger.error(
                f"Failed to upload artifact {path}: HTTP {e.response.status_code} - {e}"
            )
            return False
        except httpx.RequestError as e:
            logger.error(f"Failed to upload artifact {path}: {e}")
            return False

    async def publish_failure(
        self,
        correlation_id: UUID | None,
        file_id: str,
        error_code: str,
        details: str,
        storage_path: str,
    ) -> None:
        _now = datetime.now(timezone.utc)
        failure_event = FileAnalysisFailedEvent(
            messageId=uuid4(),
            correlationId=correlation_id,
            messageType=[
                "urn:message:Maliev.MessagingContracts.Contracts.Geometry:FileAnalysisFailedEvent"
            ],
            message=FileAnalysisFailedMessageBody(
                messageId=uuid4(),
                messageName="FileAnalysisFailedEvent",
                messageType=MessageTypeEnum.Event,
                messageVersion="1.0.0",
                publishedBy="GeometryService",
                consumedBy=["IntranetBff"],
                correlationId=correlation_id,
                causationId=None,
                occurredAtUtc=_now,
                isPublic=False,
                payload=FileAnalysisFailedPayload(
                    fileId=file_id,
                    storagePath=storage_path,
                    errorCode=error_code,
                    details=details,
                ),
            ),
        )
        await self.publish_event(
            failure_event, "maliev.geometryservice.v1.analysis.failed"
        )

    async def start(self) -> None:
        await self.connect()
        if self.queue is None:
            raise RuntimeError("Queue not initialized")

        # Bind queue to the upload service event
        if self.exchange is None:
            raise RuntimeError("Exchange not initialized")

        await self.queue.bind(
            self.exchange, routing_key="maliev.uploadservice.v1.upload.completed"
        )

        await self.queue.consume(self.process_message)
        logger.info("Consumer started and waiting for messages...")


def _extract_worker_diagnostics(file_id: UUID) -> str:
    """Extract recent worker crash logs from temp directory.

    Args:
        file_id: File ID to search for in worker log names

    Returns:
        Concatenated log contents as string, or "No worker logs found"
    """
    import glob

    logs = []
    # Search for recent worker log files
    log_pattern = f"/tmp/worker_*_{file_id.hex[:8]}*.log"
    for log_file in glob.glob(log_pattern):  # noqa: PTH207
        try:
            with open(log_file) as f:  # noqa: PTH123
                logs.append(f"=== {log_file} ===\n{f.read()}")
        except Exception:
            pass

    return "\n".join(logs) if logs else "No worker logs found"
