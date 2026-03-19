import asyncio
import base64
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import aio_pika
import aio_pika.abc
import httpx

from src.core.config import settings
from src.core.geometry import GeometryProcessor
from src.core.observability import tracer
from src.core.schemas import (
    FileAnalysisFailedEvent,
    FileAnalysisFailedMessageBody,
    FileAnalysisFailedPayload,
    FileAnalyzedEvent,
    FileAnalyzedMessageBody,
    FileAnalyzedPayload,
    FileUploadedEvent,
    MessageTypeEnum,
    PreviewImagesGeneratedEvent,
    PreviewImagesGeneratedMessageBody,
    PreviewImagesGeneratedPayload,
    PreviewImagesMessage,
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
        | PreviewImagesGeneratedEvent,
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
                    supported_exts = ["igs", "iges", "step", "stp", "stl", "obj", "3mf"]

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

                        # 3. Analyze geometry
                        (
                            metrics,
                            glb_bytes,
                            thumb_bytes,
                            preview_images,
                        ) = await self.geometry_processor.analyze_async(
                            file_stream, file_ext
                        )

                        # 4. Upload artifacts
                        glb_path = None
                        thumb_path = None
                        parent_upload_id = inner_msg.upload_id

                        if glb_bytes:
                            glb_path = f"{inner_msg.storage_path}_viewer.glb"
                            await self.upload_artifact(
                                glb_bytes,
                                glb_path,
                                "model/gltf-binary",
                                parent_upload_id,
                            )

                        if thumb_bytes:
                            thumb_path = f"{inner_msg.storage_path}_thumb.png"
                            await self.upload_artifact(
                                thumb_bytes, thumb_path, "image/png", parent_upload_id
                            )

                        # 4b. Upload preview images
                        preview_paths = {}
                        if preview_images:
                            for side, image_bytes in preview_images.items():
                                if image_bytes:
                                    preview_path = (
                                        f"{inner_msg.storage_path}_preview_{side}.png"
                                    )
                                    await self.upload_artifact(
                                        image_bytes,
                                        preview_path,
                                        "image/png",
                                        parent_upload_id,
                                    )
                                    preview_paths[side] = preview_path

                        # 5. Publish Success
                        _now = datetime.now(timezone.utc)
                        success_event = FileAnalyzedEvent(
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
                                    thumbnailStoragePath=thumb_path,
                                ),
                            ),
                        )
                        await self.publish_event(
                            success_event,
                            "maliev.geometryservice.v1.analysis.completed",
                        )

                        # 6. Publish Preview Images Generated Event
                        if preview_paths:
                            _preview_now = datetime.now(timezone.utc)
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
                                    occurredAtUtc=_preview_now,
                                    isPublic=False,
                                    payload=PreviewImagesGeneratedPayload(
                                        storagePath=inner_msg.storage_path,
                                        previewImages=PreviewImagesMessage(
                                            front_256=preview_paths.get("front_256"),
                                            back_256=preview_paths.get("back_256"),
                                            left_256=preview_paths.get("left_256"),
                                            right_256=preview_paths.get("right_256"),
                                            top_256=preview_paths.get("top_256"),
                                            bottom_256=preview_paths.get("bottom_256"),
                                            iso_256=preview_paths.get("iso_256"),
                                        ),
                                        generatedAt=_preview_now,
                                    ),
                                ),
                            )
                            await self.publish_event(
                                preview_event,
                                "maliev.geometryservice.v1.preview-images.generated",
                            )
                            logger.info(
                                "Successfully generated preview images",
                                extra={
                                    "file.id": str(file_id),
                                    "storage_path": inner_msg.storage_path,
                                    "preview_paths": str(list(preview_paths.keys())),
                                },
                            )

                        extra: dict[str, Any] = {
                            "file.id": str(file_id),
                            "volume_cm3": metrics.volume_cm3,
                            "surface_area_cm2": metrics.surface_area_cm2,
                            "bounding_box": f"{metrics.bounding_box.x} x {metrics.bounding_box.y} x {metrics.bounding_box.z}",
                            "glb_path": glb_path,
                        }
                        if thumb_path is not None:
                            extra["thumb_path"] = thumb_path
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
