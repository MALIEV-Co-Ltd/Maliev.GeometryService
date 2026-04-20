import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import psutil
import sys
import tempfile
import threading
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

# Fix PYTHONPATH for child processes spawned by ProcessPoolExecutor
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
from src.core.observability import tracer
from src.core.schemas import (
    BodyInfo,
    BoundingBox as SchemaBoundingBox,
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
from src.infrastructure.storage import IStorageService

logger = logging.getLogger(__name__)

# RSS self-defense: if the parent process crosses this threshold, force a
# gc.collect() before accepting more work.  Not a hard limit — just a nudge
# to reclaim unreferenced pages before they compound.
_RSS_GC_THRESHOLD_MB = 3_000  # 3 GB — conservative; process limit is typically 4-8 GB


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
    return rss_mb


# T5d: single source of truth for Phase 2 task budgets.
# All per-task timeouts are derived from this constant so log messages
# and actual deadline are always in sync.
_THUMBNAIL_BUDGET_S = 180   # single/multi-body thumbnail
_GLB_BUDGET_S = 180         # viewer GLB export
_PREVIEW_BUDGET_S = 60      # 7-view preview generation (reduced for faster failure detection)


def _shutdown_executor_gracefully(processor: "GeometryProcessor", timeout_seconds: int = 10) -> None:
    """Gracefully shutdown a GeometryProcessor's executors with timeout.

    This function ensures that executors are properly shut down with a timeout,
    preventing orphaned processes. It tries graceful shutdown first, then falls
    back to force shutdown if needed.

    Args:
        processor: The GeometryProcessor to shutdown
        timeout_seconds: Maximum time to wait for shutdown to complete
    """
    def _do_shutdown():
        try:
            # First, try to shutdown with wait=True but with timeout
            if hasattr(processor, 'executor'):
                try:
                    processor.executor.shutdown(wait=True, timeout=timeout_seconds)
                except Exception as e:
                    logger.warning(f"Executor shutdown failed: {e}")
                    # Force shutdown if graceful fails
                    try:
                        processor.executor.shutdown(wait=False)
                    except Exception:
                        pass

            if hasattr(processor, 'dfm_executor'):
                try:
                    processor.dfm_executor.shutdown(wait=True, timeout=timeout_seconds)
                except Exception as e:
                    logger.warning(f"DFM executor shutdown failed: {e}")
                    try:
                        processor.dfm_executor.shutdown(wait=False)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Exception during shutdown: {e}")

    # Run shutdown in background thread with timeout
    shutdown_thread = threading.Thread(target=_do_shutdown, daemon=False)
    shutdown_thread.start()

    # Wait for shutdown to complete or timeout
    shutdown_thread.join(timeout=timeout_seconds + 5)

    if shutdown_thread.is_alive():
        logger.warning("Shutdown thread still running after timeout - executors may leak")


class UploadConsumer:
    def __init__(
        self, storage_service: IStorageService, geometry_processor: GeometryProcessor
    ):
        self.storage_service = storage_service
        self.geometry_processor = geometry_processor
        self._token_provider = ServiceAccountTokenProvider()
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None
        self.queue: aio_pika.abc.AbstractRobustQueue | None = None
        self.exchange: aio_pika.abc.AbstractRobustExchange | None = None
        # T4a: single shared client — avoids a TLS handshake per artifact upload.
        # Limits is set high enough to saturate the upload fan-out (4 Phase-2
        # tasks + N overlay GLBs).  keepalive_expiry keeps connections warm
        # between messages in a slow queue.
        self._http_client: httpx.AsyncClient = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=5.0),
        )

    async def connect(self) -> None:
        max_retries = 10
        base_delay = 1.0
        max_delay = 30.0

        for attempt in range(max_retries):
            try:
                self.connection = await aio_pika.connect_robust(settings.RABBITMQ_URI)
                self.channel = await self.connection.channel()
                await self.channel.set_qos(prefetch_count=2)

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
                logger.info("Successfully connected to RabbitMQ")
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
                        "blend",
                        "fbx",
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

                    # Check Content-Length header to avoid wasting bandwidth on oversized files
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
                        # Add total timeout so the consumer never hangs waiting for a dead worker.
                        # The cascadio thread-based timeout in geometry.py abandons the C-extension
                        # thread after timeout_seconds; this outer timeout is a safety net.
                        PHASE1_TIMEOUT_SECONDS = (
                            300  # 5 minutes max for the whole phase
                        )
                        loop = asyncio.get_running_loop()
                        executor = self.geometry_processor.executor
                        logger.info(
                            "Starting Phase 1 metrics computation",
                            extra={
                                "event": "phase1_start",
                                "file_id": str(file_id),
                                "extension": file_ext,
                            },
                        )
                        try:
                            metrics_result = await asyncio.wait_for(
                                loop.run_in_executor(
                                    executor, _compute_metrics_worker, data, file_ext
                                ),
                                timeout=PHASE1_TIMEOUT_SECONDS,
                            )
                            rss_mb = _check_rss_and_maybe_gc("post-phase1")
                            logger.info(
                                "Phase 1 metrics computation complete",
                                extra={
                                    "event": "phase1_complete",
                                    "file_id": str(file_id),
                                    "volume_cm3": metrics_result["volume_cm3"],
                                    "rss_mb": round(rss_mb, 1),
                                },
                            )
                        except asyncio.TimeoutError:
                            logger.error(
                                f"Phase 1 timed out after {PHASE1_TIMEOUT_SECONDS}s — replacing executor",
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
                            # ProcessPoolExecutor worker died unexpectedly (e.g., gmsh crash).
                            # Try trimesh-only fallback so we don't fail the whole job.
                            logger.warning(
                                f"Phase 1 worker crashed — trying trimesh-only fallback: {ex}",
                                extra={
                                    "event": "phase1_worker_crash",
                                    "file_id": str(file_id),
                                },
                            )
                            _old_broken = self.geometry_processor
                            _shutdown_executor_gracefully(_old_broken, timeout_seconds=10)
                            self.geometry_processor = GeometryProcessor()
                            executor = self.geometry_processor.executor
                            # Use trimesh directly in this process (no executor) for metrics only.
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

                        metrics = GeometryMetrics(
                            volumeCm3=metrics_result["volume_cm3"],
                            supportVolumeCm3=metrics_result["support_volume_cm3"],
                            surfaceAreaCm2=metrics_result["surface_area_cm2"],
                            boundingBox=BoundingBox(**metrics_result["bounding_box"]),
                            isManifold=metrics_result["is_manifold"],
                            triangleCount=metrics_result["triangle_count"],
                            eulerNumber=metrics_result["euler_number"],
                        )

                        # Extract body metadata
                        body_count = metrics_result.get("body_count", 1)
                        body_names = metrics_result.get("body_names", [])
                        body_volumes = metrics_result.get("body_volumes_cm3", [])
                        body_infos: list[BodyInfo] | None = None
                        if body_count > 1 and body_names:
                            body_infos = [
                                BodyInfo(
                                    index=i,
                                    name=name,
                                    volume_cm3=body_volumes[i] if i < len(body_volumes) else None,
                                    bbox_min=None,
                                    bbox_max=None,
                                )
                                for i, name in enumerate(body_names)
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
                                    bodyCount=body_count if body_count > 1 else None,
                                    bodies=body_infos,
                                ),
                            ),
                        )
                        await self.publish_event(
                            metrics_event,
                            "maliev.geometryservice.v1.metrics.ready",
                        )

                        # 3b. Phase 2 — 4 independent parallel workers using GLB directly
                        # For CAD files (STEP/IGES), cascadio produces GLB — use it directly
                        # For pure STL/OBJ uploads, fall back to original bytes
                        cad_glb_bytes = metrics_result.get("cad_glb_bytes")
                        original_stl_bytes = metrics_result.get("mesh_stl_bytes")  # For pure STL uploads
                        body_count = metrics_result.get("body_count", 1)
                        body_names = metrics_result.get("body_names", [])
                        mesh_stl_bytes_dict = metrics_result.get("mesh_stl_bytes_dict", {})  # For DFM only

                        # Debug logging
                        logger.info(
                            f"Phase 2 inputs: cad_glb_bytes={len(cad_glb_bytes) if cad_glb_bytes else None}, "
                            f"original_stl_bytes={len(original_stl_bytes) if original_stl_bytes else None}, "
                            f"file_ext={file_ext}, body_count={body_count}, has_bodies={bool(mesh_stl_bytes_dict)}"
                        )

                        if not cad_glb_bytes and not original_stl_bytes:
                            logger.warning(
                                "No GLB or STL bytes available — skipping Phase 2",
                                extra={"event": "phase2_skip", "file_id": str(file_id)},
                            )
                            try:
                                await self.publish_failure(
                                    correlation_id,
                                    file_id,
                                    "GEOMETRY_NO_RESULT",
                                    "Phase 1 produced no GLB or STL bytes",
                                    inner_msg.storage_path,
                                )
                            except Exception as pub_err:
                                logger.warning(
                                    "Failed to publish GEOMETRY_NO_RESULT for Phase 2 skip: %s",
                                    pub_err,
                                )
                        else:
                            upload_id = inner_msg.upload_id
                            _glb_published = [False]

                            # Create message-scoped temp directory for Phase 2 artifacts
                            temp_dir: str | None = None
                            try:
                                temp_dir = tempfile.mkdtemp(prefix="geom_")
                                logger.info(
                                    f"Created temp directory for Phase 2: {temp_dir}",
                                    extra={"event": "phase2_temp_dir", "file_id": str(file_id)},
                                )

                                # Write CAD GLB for thumbnail/GLB/previews (fast, multi-body aware)
                                cad_glb_path: str | None = None
                                if cad_glb_bytes:
                                    cad_glb_path = os.path.join(temp_dir, "cad.glb")
                                    with open(cad_glb_path, "wb") as f:
                                        f.write(cad_glb_bytes)

                                # Write original CAD file for DFM B-Rep analysis (only for STEP/IGES)
                                cad_path_for_dfm: str | None = None
                                cad_ext = file_ext.strip(".")
                                if cad_ext in ("step", "stp", "igs", "iges") and data:
                                    cad_path_for_dfm = os.path.join(temp_dir, f"original.{cad_ext}")
                                    with open(cad_path_for_dfm, "wb") as f:
                                        f.write(data)

                                # ── Fix: release large in-memory buffers immediately ──────────
                                # `data` (original uploaded file, 10-200 MB) and `cad_glb_bytes`
                                # (converted GLB, up to 100 MB) are both now flushed to disk.
                                # Setting them to None lets GC reclaim the bytes while the four
                                # Phase 2 tasks run (up to 300 s).  With prefetch_count=2, a
                                # second large file can arrive before Phase 2 finishes; without
                                # this release, both payloads co-exist in the parent process.
                                #
                                # DO NOT clear original_stl_bytes here — _run_dfm uses it as an
                                # early-exit check for pure STL uploads (cad_glb_path = None).
                                # DO NOT clear mesh_stl_bytes_dict here — _run_dfm reads it to
                                # write per-body temp STL files; it frees the dict itself after.
                                data = None  # noqa: F841
                                cad_glb_bytes = None  # noqa: F841

                                logger.info(
                                    f"Phase 2 temp files written: CAD GLB={bool(cad_glb_path)}, "
                                    f"original CAD={bool(cad_path_for_dfm)}",
                                    extra={"event": "phase2_temp_files", "file_id": str(file_id)},
                                )

                                # ------------------------------------------------------------------
                                # Phase 2 fan-out: 4 independent tasks, each publishes when done
                                # ------------------------------------------------------------------

                                async def _run_small_thumbnail() -> None:
                                    try:
                                        # Use extended timeout for multi-body files (they have larger GLBs)
                                        # Multi-body GLBs can be 100MB+ vs 1-5MB for single body
                                        small_timeout = 180 if (body_count and body_count > 1) else 60
                                        thumb = await asyncio.wait_for(
                                            loop.run_in_executor(
                                                executor,
                                                _render_thumbnail_worker,
                                                cad_glb_path,
                                            ),
                                            timeout=small_timeout,
                                        )
                                        if not thumb:
                                            return
                                        thumb_path = f"{inner_msg.storage_path}_thumbnail_small.webp"
                                        thumb_uploaded = await self.upload_artifact(
                                            thumb, thumb_path, "image/webp", upload_id
                                        )
                                        if not thumb_uploaded:
                                            logger.warning(
                                                "Small thumbnail upload failed — skipping event",
                                                extra={"file_id": str(file_id)},
                                            )
                                            return
                                        _now = datetime.now(timezone.utc)
                                        thumb_event = SmallThumbnailReadyEvent(
                                            messageId=uuid4(),
                                            correlationId=correlation_id,
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
                                                correlationId=correlation_id,
                                                causationId=None,
                                                occurredAtUtc=_now,
                                                isPublic=False,
                                                payload=SmallThumbnailReadyPayload(
                                                    fileId=file_id,
                                                    storagePath=inner_msg.storage_path,
                                                    thumbnailStoragePath=thumb_path,
                                                ),
                                            ),
                                        )
                                        await self.publish_event(
                                            thumb_event,
                                            "maliev.geometryservice.v1.thumbnail.small.ready",
                                        )
                                        logger.info(
                                            "Small thumbnail published",
                                            extra={"event": "thumbnail_small_published", "file_id": str(file_id)},
                                        )
                                    except asyncio.TimeoutError:
                                        # Log timeout with actual duration and file details
                                        logger.warning(
                                            f"Small thumbnail timed out after {small_timeout}s (file: {inner_msg.file_name}, size: {inner_msg.file_size} bytes, bodies: {body_count})",
                                            extra={"event": "thumbnail_small_timeout", "file_id": str(file_id)},
                                        )
                                    except Exception as e:
                                        logger.warning(
                                            f"Small thumbnail task failed (non-fatal): {e}",
                                            extra={"event": "thumbnail_small_error", "file_id": str(file_id)},
                                        )

                                async def _run_glb() -> None:
                                    try:
                                        glb = await asyncio.wait_for(
                                            loop.run_in_executor(
                                                executor,
                                                _export_glb_from_paths,
                                                cad_glb_path,
                                                file_ext,
                                            ),
                                            timeout=_GLB_BUDGET_S,
                                        )
                                        if not glb:
                                            return
                                        glb_path = f"{inner_msg.storage_path}_viewer.glb"
                                        uploaded = await self.upload_artifact(
                                            glb, glb_path, "model/gltf-binary", upload_id
                                        )
                                        if not uploaded:
                                            logger.warning(
                                                "GLB upload failed — skipping FileAnalyzedEvent",
                                                extra={"file_id": str(file_id)},
                                            )
                                            return
                                        _now = datetime.now(timezone.utc)
                                        glb_event = FileAnalyzedEvent(
                                            messageId=uuid4(),
                                            correlationId=correlation_id,
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
                                                correlationId=correlation_id,
                                                causationId=None,
                                                occurredAtUtc=_now,
                                                isPublic=False,
                                                payload=FileAnalyzedPayload(
                                                    fileId=file_id,
                                                    metrics=metrics,
                                                    processedAt=_now,
                                                    glbStoragePath=glb_path,
                                                    thumbnailStoragePath=None,
                                                    storagePath=inner_msg.storage_path,
                                                    dfmReport=None,
                                                    bodyCount=body_count if body_count > 1 else None,
                                                    bodies=body_infos,
                                                ),
                                            ),
                                        )
                                        await self.publish_event(
                                            glb_event,
                                            "maliev.geometryservice.v1.analysis.completed",
                                        )
                                        _glb_published[0] = True
                                        logger.info(
                                            "GLB published",
                                            extra={"event": "glb_published", "file_id": str(file_id)},
                                        )
                                    except asyncio.TimeoutError:
                                        logger.warning(
                                            f"GLB export timed out after {_GLB_BUDGET_S}s",
                                            extra={"event": "glb_timeout", "file_id": str(file_id)},
                                        )
                                    except Exception as e:
                                        logger.warning(
                                            f"GLB task failed (non-fatal): {e}",
                                            extra={"event": "glb_error", "file_id": str(file_id)},
                                        )

                                async def _run_previews() -> None:
                                    try:
                                        preview_images = await asyncio.wait_for(
                                            loop.run_in_executor(
                                                executor,
                                                _render_preview_worker,
                                                cad_glb_path,
                                            ),
                                            timeout=_PREVIEW_BUDGET_S,
                                        )

                                        preview_paths: dict[str, str] = {}
                                        thumbnail_large_path: str | None = None

                                        for side, image_bytes in preview_images.items():
                                            if side in ("thumbnail_small", "thumbnail_large"):
                                                continue
                                            if image_bytes:
                                                preview_path = f"{inner_msg.storage_path}_preview_{side}.webp"
                                                ok = await self.upload_artifact(
                                                    image_bytes,
                                                    preview_path,
                                                    "image/webp",
                                                    upload_id,
                                                )
                                                if ok:
                                                    preview_paths[side] = preview_path

                                        thumbnail_large_bytes = preview_images.get("thumbnail_large")
                                        if thumbnail_large_bytes:
                                            thumbnail_large_path = f"{inner_msg.storage_path}_thumbnail_large.webp"
                                            large_ok = await self.upload_artifact(
                                                thumbnail_large_bytes,
                                                thumbnail_large_path,
                                                "image/webp",
                                                upload_id,
                                            )
                                            if not large_ok:
                                                thumbnail_large_path = None

                                        _now = datetime.now(timezone.utc)
                                        preview_event = PreviewImagesGeneratedEvent(
                                            messageId=uuid4(),
                                            correlationId=correlation_id,
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
                                                correlationId=correlation_id,
                                                causationId=None,
                                                occurredAtUtc=_now,
                                                isPublic=False,
                                                payload=PreviewImagesGeneratedPayload(
                                                    storagePath=inner_msg.storage_path,
                                                    previewImages=PreviewImagesMessage(
                                                        frontSmall=preview_paths.get("front_small"),
                                                        backSmall=preview_paths.get("back_small"),
                                                        leftSmall=preview_paths.get("left_small"),
                                                        rightSmall=preview_paths.get("right_small"),
                                                        topSmall=preview_paths.get("top_small"),
                                                        bottomSmall=preview_paths.get("bottom_small"),
                                                        thumbnailSmall=None,
                                                        thumbnailLarge=thumbnail_large_path,
                                                    ),
                                                    generatedAt=_now,
                                                ),
                                            ),
                                        )
                                        await self.publish_event(
                                            preview_event,
                                            "maliev.geometryservice.v1.preview-images.generated",
                                        )
                                        logger.info(
                                            "Previews published",
                                            extra={"event": "previews_published", "file_id": str(file_id)},
                                        )
                                    except asyncio.TimeoutError:
                                        logger.warning(
                                            "Previews timed out after 300s",
                                            extra={"event": "previews_timeout", "file_id": str(file_id)},
                                        )
                                    except Exception as e:
                                        logger.warning(
                                            f"Previews task failed (non-fatal): {e}",
                                            extra={"event": "previews_error", "file_id": str(file_id)},
                                        )

                                # ------------------------------------------------------------------
                                # Fan-out: all three tasks start immediately, each publishes
                                # when done. 15-minute hard deadline prevents runaway.
                                # ------------------------------------------------------------------
                                PHASE2_HARD_DEADLINE_SECONDS = 300  # 5 minutes for all Phase 2 tasks (thumbnail, GLB, previews)
                                logger.info(
                                    "Phase 2 fan-out starting (thumbnail, GLB, previews)",
                                    extra={"event": "phase2_fanout_start", "file_id": str(file_id)},
                                )

                                # Track which tasks have completed for progressive cleanup
                                completed_tasks = set()

                                async def _run_with_cleanup(task_name: str, task_coro, cleanup_fn=None) -> None:
                                    """Run a task and optionally clean up resources after completion."""
                                    try:
                                        await task_coro
                                        completed_tasks.add(task_name)
                                        logger.info(
                                            f"Task {task_name} completed ({len(completed_tasks)}/3)",
                                            extra={"event": f"phase2_task_{task_name}_complete", "file_id": str(file_id)}
                                        )

                                        # Progressive cleanup: remove temp files as soon as tasks finish
                                        if cleanup_fn and temp_dir:
                                            try:
                                                cleanup_fn()
                                                logger.info(
                                                    f"Progressive cleanup after {task_name}",
                                                    extra={"event": "phase2_progressive_cleanup", "task": task_name}
                                                )
                                            except Exception as cleanup_err:
                                                logger.warning(
                                                    f"Progressive cleanup failed after {task_name}: {cleanup_err}",
                                                    extra={"event": "phase2_cleanup_error", "task": task_name}
                                                )
                                    except Exception as e:
                                        completed_tasks.add(task_name)
                                        logger.warning(
                                            f"Task {task_name} failed: {e}",
                                            extra={"event": f"phase2_task_{task_name}_failed", "file_id": str(file_id)}
                                        )

                                def cleanup_cad_glb():
                                    """Clean up CAD GLB after ALL Phase 2 tasks complete.

                                    cad.glb is needed by rendering tasks (thumb, previews, glb).
                                    Do NOT delete it until every task is done.
                                    """
                                    if len(completed_tasks) >= 3 and cad_glb_path:
                                        try:
                                            if os.path.exists(cad_glb_path):
                                                size_mb = os.path.getsize(cad_glb_path)/1024/1024
                                                os.unlink(cad_glb_path)
                                                logger.info(
                                                    f"Removed cad.glb ({size_mb:.1f} MB freed)",
                                                    extra={"event": "phase2_cad_glb_removed"}
                                                )
                                            else:
                                                logger.debug(f"cad.glb already removed or never created")
                                        except Exception as e:
                                            logger.warning(f"Failed to remove cad.glb: {e}")

                                def cleanup_cad_file():
                                    """Clean up original CAD file after Phase 2 completes."""
                                    # Original CAD file no longer needed after rendering tasks finish
                                    if len(completed_tasks) >= 3 and cad_path_for_dfm:
                                        try:
                                            if os.path.exists(cad_path_for_dfm):
                                                size_mb = os.path.getsize(cad_path_for_dfm)/1024/1024
                                                os.unlink(cad_path_for_dfm)
                                                logger.info(
                                                    f"Removed original.{cad_ext} ({size_mb:.1f} MB freed)",
                                                    extra={"event": "phase2_cad_file_removed"}
                                                )
                                            else:
                                                logger.debug(f"original.{cad_ext} already removed")
                                        except Exception as e:
                                            logger.warning(f"Failed to remove original.{cad_ext}: {e}")

                                # Create tasks with progressive cleanup hooks.
                                # Shared file (cad.glb) is only deleted when ALL 4 tasks complete —
                                # the cleanup function checks len(completed_tasks) >= 4.
                                # Per-task files (original CAD) are cleaned up after DFM only.
                                def cleanup_shared():
                                    """Run shared-file cleanup; safe to call from any task."""
                                    cleanup_cad_glb()
                                    cleanup_cad_file()

                                tasks = {
                                    "thumb": asyncio.create_task(
                                        _run_with_cleanup("thumb", _run_small_thumbnail(), cleanup_shared)
                                    ),
                                    "glb": asyncio.create_task(
                                        _run_with_cleanup("glb", _run_glb(), cleanup_shared)
                                    ),
                                    "previews": asyncio.create_task(
                                        _run_with_cleanup("previews", _run_previews(), cleanup_shared)
                                    ),
                                }

                                done, pending = await asyncio.wait(
                                    tasks.values(),
                                    timeout=PHASE2_HARD_DEADLINE_SECONDS,
                                )
                                for t in pending:
                                    t.cancel()
                                    logger.warning(
                                        "Phase 2 task cancelled after deadline",
                                        extra={"event": "phase2_task_cancelled", "file_id": str(file_id)},
                                    )

                                rss_mb = _check_rss_and_maybe_gc("post-phase2")
                                logger.info(
                                    "Phase 2 complete (all tasks finished or timed out)",
                                    extra={
                                        "event": "phase2_complete",
                                        "file_id": str(file_id),
                                        "rss_mb": round(rss_mb, 1),
                                    },
                                )

                                # Safety net: if no GLB was published, fail explicitly
                                if not _glb_published[0]:
                                    logger.warning(
                                        "Phase 2 completed without publishing a GLB artifact — "
                                        "publishing GEOMETRY_NO_RESULT failure event",
                                        extra={
                                            "event": "phase2_no_result",
                                            "file_id": str(file_id),
                                        },
                                    )
                                    try:
                                        await self.publish_failure(
                                            correlation_id,
                                            file_id,
                                            "GEOMETRY_NO_RESULT",
                                            "Phase 2 did not produce a GLB artifact",
                                            inner_msg.storage_path,
                                        )
                                    except Exception as pub_err:
                                        logger.warning(
                                            "Failed to publish GEOMETRY_NO_RESULT: %s",
                                            pub_err,
                                            extra={
                                                "event": "phase2_no_result_publish_error",
                                                "file_id": str(file_id),
                                            },
                                        )

                            finally:
                                # Clean up temp directory
                                if temp_dir and os.path.exists(temp_dir):
                                    try:
                                        import shutil
                                        shutil.rmtree(temp_dir)
                                        logger.info(
                                            f"Cleaned up temp directory: {temp_dir}",
                                            extra={"event": "phase2_temp_cleanup", "file_id": str(file_id)},
                                        )
                                    except Exception as cleanup_err:
                                        logger.warning(
                                            f"Failed to clean up temp directory {temp_dir}: {cleanup_err}",
                                            extra={"event": "phase2_temp_cleanup_error", "file_id": str(file_id)},
                                        )

                                extra: dict[str, Any] = {
                                    "file.id": str(file_id),
                                    "volume_cm3": metrics.volume_cm3,
                                    "surface_area_cm2": metrics.surface_area_cm2,
                                    "bounding_box": f"{metrics.bounding_box.x} x {metrics.bounding_box.y} x {metrics.bounding_box.z}",
                                }
                                logger.info(
                                    "Successfully analyzed file",
                                    extra=extra,
                                )

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

    async def validate_file_size_before_download(self, url: str) -> bool:
        """Check Content-Length header before downloading to reject oversized files early.

        Returns True if file size is within limits, False if exceeds MAX_FILE_SIZE_MB.
        """
        try:
            # T4a: reuse the shared client instead of creating a fresh one.
            response = await self._http_client.head(url)
            content_length = response.headers.get("Content-Length")
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                if size_mb > settings.MAX_FILE_SIZE_MB:
                    logger.warning(
                        f"File exceeds size limit: {size_mb:.1f}MB > {settings.MAX_FILE_SIZE_MB}MB - rejecting early",
                        extra={"event": "file_size_early_rejection", "size_mb": size_mb}
                    )
                    return False
                else:
                    logger.info(
                        f"File size check passed: {size_mb:.1f}MB",
                        extra={"event": "file_size_validated", "size_mb": size_mb}
                    )
                    return True
            else:
                # Content-Length not available, proceed with download
                logger.debug("Content-Length header not available, skipping early size check")
                return True
        except Exception as e:
            logger.warning(f"Failed to check file size before download: {e}, proceeding anyway")
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
        self, data: bytes, path: str, content_type: str, parent_upload_id: str = ""
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
            logger.info(
                f"Artifact uploaded successfully: {result.get('storagePath')}"
            )
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
    for log_file in glob.glob(log_pattern):
        try:
            with open(log_file, "r") as f:
                logs.append(f"=== {log_file} ===\n{f.read()}")
        except Exception:
            pass

    return "\n".join(logs) if logs else "No worker logs found"
