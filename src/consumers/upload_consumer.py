import asyncio
import base64
import io
import json
import logging
import os
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import aio_pika
import aio_pika.abc
import httpx

from src.core.config import settings
from src.core.geometry import (
    BoundingBox,
    CncDfmReport,
    FdmDfmReport,
    GeometryMetrics,
    GeometryProcessor,
    SlaDfmReport,
    _compute_dfm_worker,
    _compute_metrics_worker,
    _export_glb_worker,
    _render_large_preview_worker,
    _render_small_thumbnail_worker,
    compute_metrics_trimesh_only,
)
from src.core.observability import tracer
from src.core.schemas import (
    DfmAnalysisReadyEvent,
    DfmAnalysisReadyMessageBody,
    DfmAnalysisReadyPayload,
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
from src.infrastructure.storage import IStorageService

logger = logging.getLogger(__name__)


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

    async def connect(self) -> None:
        max_retries = 10
        base_delay = 1.0
        max_delay = 30.0

        for attempt in range(max_retries):
            try:
                self.connection = await aio_pika.connect_robust(settings.RABBITMQ_URI)
                self.channel = await self.connection.channel()
                await self.channel.set_qos(prefetch_count=1)

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
        | DfmAnalysisReadyEvent
        | SmallThumbnailReadyEvent,
        routing_key: str,
    ) -> None:
        if self.exchange is None:
            raise RuntimeError("Exchange not initialized")

        # model_dump_json(by_alias=True) ensures camelCase for MassTransit
        message_body = event.model_dump_json(by_alias=True).encode()
        await self.exchange.publish(
            aio_pika.Message(
                body=message_body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=routing_key,
        )

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

                    # 1. Download file with retry logic

                    if not inner_msg.download_url:
                        raise ValueError("MISSING_DOWNLOAD_URL")

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
                            logger.info(
                                "Phase 1 metrics computation complete",
                                extra={
                                    "event": "phase1_complete",
                                    "file_id": str(file_id),
                                    "volume_cm3": metrics_result["volume_cm3"],
                                },
                            )
                        except asyncio.TimeoutError:
                            logger.error(
                                f"Phase 1 timed out after {PHASE1_TIMEOUT_SECONDS}s — killing executor",
                                extra={
                                    "event": "phase1_timeout",
                                    "file_id": str(file_id),
                                },
                            )
                            # Forcefully shutdown the executor so ProcessPoolExecutor
                            # kills all workers and recovers for the next message.
                            self.geometry_processor.shutdown()
                            self.geometry_processor = GeometryProcessor()
                            raise ValueError("GEOMETRY_PROCESS_TIMEOUT")
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
                            self.geometry_processor.shutdown()
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
                                    # smallThumbnailStoragePath removed — thumbnail is now a separate event
                                ),
                            ),
                        )
                        await self.publish_event(
                            metrics_event,
                            "maliev.geometryservice.v1.metrics.ready",
                        )

                        # 3b. Phase 2 — 4 independent parallel workers
                        mesh_stl_bytes = metrics_result.get("mesh_stl_bytes")
                        cad_glb_bytes = metrics_result.get("cad_glb_bytes")

                        if not mesh_stl_bytes:
                            logger.warning(
                                "No STL bytes available — skipping Phase 2",
                                extra={"event": "phase2_skip", "file_id": str(file_id)},
                            )
                        else:
                            upload_id = inner_msg.upload_id

                            async def _run_small_thumbnail() -> None:
                                try:
                                    thumb = await asyncio.wait_for(
                                        loop.run_in_executor(
                                            executor,
                                            _render_small_thumbnail_worker,
                                            mesh_stl_bytes,
                                        ),
                                        timeout=120,
                                    )
                                    if not thumb:
                                        return
                                    thumb_path = (
                                        f"{inner_msg.storage_path}_thumbnail_small.webp"
                                    )
                                    await self.upload_artifact(
                                        thumb, thumb_path, "image/webp", upload_id
                                    )
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
                                        extra={
                                            "event": "thumbnail_small_published",
                                            "file_id": str(file_id),
                                        },
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"Small thumbnail task failed (non-fatal): {e}",
                                        extra={
                                            "event": "thumbnail_small_error",
                                            "file_id": str(file_id),
                                        },
                                    )

                            async def _run_glb() -> None:
                                try:
                                    glb = await asyncio.wait_for(
                                        loop.run_in_executor(
                                            executor,
                                            _export_glb_worker,
                                            mesh_stl_bytes,
                                            cad_glb_bytes,
                                        ),
                                        timeout=120,
                                    )
                                    if not glb:
                                        return
                                    glb_path = f"{inner_msg.storage_path}_viewer.glb"
                                    await self.upload_artifact(
                                        glb, glb_path, "model/gltf-binary", upload_id
                                    )
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
                                            ),
                                        ),
                                    )
                                    await self.publish_event(
                                        glb_event,
                                        "maliev.geometryservice.v1.analysis.completed",
                                    )
                                    logger.info(
                                        "GLB published",
                                        extra={
                                            "event": "glb_published",
                                            "file_id": str(file_id),
                                        },
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"GLB task failed (non-fatal): {e}",
                                        extra={
                                            "event": "glb_error",
                                            "file_id": str(file_id),
                                        },
                                    )

                            async def _run_dfm() -> None:
                                try:
                                    reports = await asyncio.wait_for(
                                        loop.run_in_executor(
                                            executor,
                                            _compute_dfm_worker,
                                            mesh_stl_bytes,
                                        ),
                                        timeout=300,
                                    )
                                    _now = datetime.now(timezone.utc)
                                    fdm_raw = reports.get("FDM")
                                    sla_raw = reports.get("SLA")
                                    cnc_raw = reports.get("CNC")
                                    dfm_event = DfmAnalysisReadyEvent(
                                        messageId=uuid4(),
                                        correlationId=correlation_id,
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
                                            correlationId=correlation_id,
                                            causationId=None,
                                            occurredAtUtc=_now,
                                            isPublic=False,
                                            payload=DfmAnalysisReadyPayload(
                                                fileId=file_id,
                                                storagePath=inner_msg.storage_path,
                                                fdmReport=FdmDfmReport.model_validate(
                                                    fdm_raw
                                                )
                                                if fdm_raw
                                                else None,
                                                slaReport=SlaDfmReport.model_validate(
                                                    sla_raw
                                                )
                                                if sla_raw
                                                else None,
                                                cncReport=CncDfmReport.model_validate(
                                                    cnc_raw
                                                )
                                                if cnc_raw
                                                else None,
                                                analyzedAt=_now,
                                            ),
                                        ),
                                    )
                                    await self.publish_event(
                                        dfm_event, "maliev.geometryservice.v1.dfm.ready"
                                    )
                                    logger.info(
                                        "DFM published",
                                        extra={
                                            "event": "dfm_published",
                                            "file_id": str(file_id),
                                        },
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"DFM task failed (non-fatal): {e}",
                                        extra={
                                            "event": "dfm_error",
                                            "file_id": str(file_id),
                                        },
                                    )

                            async def _run_previews() -> None:
                                try:
                                    preview_images = await asyncio.wait_for(
                                        loop.run_in_executor(
                                            executor,
                                            _render_large_preview_worker,
                                            mesh_stl_bytes,
                                        ),
                                        timeout=300,
                                    )

                                    preview_paths: dict[str, str] = {}
                                    thumbnail_large_path: str | None = None

                                    for side, image_bytes in preview_images.items():
                                        if side in (
                                            "thumbnail_small",
                                            "thumbnail_large",
                                        ):
                                            continue
                                        if image_bytes:
                                            preview_path = f"{inner_msg.storage_path}_preview_{side}.webp"
                                            await self.upload_artifact(
                                                image_bytes,
                                                preview_path,
                                                "image/webp",
                                                upload_id,
                                            )
                                            preview_paths[side] = preview_path

                                    thumbnail_large_bytes = preview_images.get(
                                        "thumbnail_large"
                                    )
                                    if thumbnail_large_bytes:
                                        thumbnail_large_path = f"{inner_msg.storage_path}_thumbnail_large.webp"
                                        await self.upload_artifact(
                                            thumbnail_large_bytes,
                                            thumbnail_large_path,
                                            "image/webp",
                                            upload_id,
                                        )

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
                                                    frontSmall=preview_paths.get(
                                                        "front_small"
                                                    ),
                                                    backSmall=preview_paths.get("back_small"),
                                                    leftSmall=preview_paths.get("left_small"),
                                                    rightSmall=preview_paths.get(
                                                        "right_small"
                                                    ),
                                                    topSmall=preview_paths.get("top_small"),
                                                    bottomSmall=preview_paths.get(
                                                        "bottom_small"
                                                    ),
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
                                        extra={
                                            "event": "previews_published",
                                            "file_id": str(file_id),
                                        },
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"Previews task failed (non-fatal): {e}",
                                        extra={
                                            "event": "previews_error",
                                            "file_id": str(file_id),
                                        },
                                    )

                            await asyncio.gather(
                                _run_small_thumbnail(),
                                _run_glb(),
                                _run_dfm(),
                                _run_previews(),
                                return_exceptions=True,
                            )
                            logger.info(
                                "Phase 2 all tasks complete",
                                extra={
                                    "event": "phase2_complete",
                                    "file_id": str(file_id),
                                },
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
                    else:
                        error_code = "FILE_CORRUPT"

                    await self.publish_failure(
                        correlation_id, file_id, error_code, str(e)
                    )
                except Exception as e:
                    logger.error(f"Error processing {file_id}: {e}")
                    await self.publish_failure(
                        correlation_id, file_id, "SYSTEM_ERROR", str(e)
                    )

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
    ) -> None:
        """Uploads an artifact (GLB/PNG) to GCS via UploadService HTTP endpoint."""
        try:
            upload_service_url = settings.UPLOAD_SERVICE_URL
            artifact_id = str(uuid4())
            token = self._token_provider.get_token()

            payload = {
                "artifactId": artifact_id,
                "parentUploadId": parent_upload_id,
                "storagePath": path,
                "contentType": content_type,
                "artifactData": base64.b64encode(data).decode("utf-8"),
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{upload_service_url}/upload/v1/uploads/artifacts",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                result = response.json()
                logger.info(
                    f"Artifact uploaded successfully: {result.get('storagePath')}"
                )

        except httpx.HTTPStatusError as e:
            logger.error(
                f"Failed to upload artifact {path}: HTTP {e.response.status_code} - {e}"
            )
            logger.warning(f"Proceeding without artifact {path}")
        except httpx.RequestError as e:
            logger.error(f"Failed to upload artifact {path}: {e}")
            logger.warning(f"Proceeding without artifact {path}")

    async def publish_failure(
        self, correlation_id: UUID | None, file_id: str, error_code: str, details: str
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
                    fileId=file_id, errorCode=error_code, details=details
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
