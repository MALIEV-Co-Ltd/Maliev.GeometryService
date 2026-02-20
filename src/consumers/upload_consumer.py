import asyncio
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import aio_pika
import aio_pika.abc

from src.core.config import settings
from src.core.geometry import GeometryProcessor
from src.core.observability import tracer
from src.core.schemas import (
    FileAnalysisFailedEvent,
    FileAnalysisFailedMessage,
    FileAnalyzedEvent,
    FileAnalyzedMessage,
    FileUploadedEvent,
)
from src.infrastructure.storage import IStorageService

logger = logging.getLogger(__name__)


class UploadConsumer:
    def __init__(
        self, storage_service: IStorageService, geometry_processor: GeometryProcessor
    ):
        self.storage_service = storage_service
        self.geometry_processor = geometry_processor
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None
        self.queue: aio_pika.abc.AbstractRobustQueue | None = None
        self.exchange: aio_pika.abc.AbstractRobustExchange | None = None

    async def connect(self) -> None:
        max_retries = 10
        base_delay = 1.0

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
                delay = base_delay * (2**attempt)
                logger.warning(
                    f"RabbitMQ connection attempt {attempt + 1}/{max_retries} "
                    f"failed, retrying in {delay}s: {e}"
                )
                await asyncio.sleep(delay)

    async def publish_event(
        self,
        event: FileAnalyzedEvent | FileAnalysisFailedEvent,
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

                    # 0. Filter by file extension before downloading
                    file_ext = Path(inner_msg.storage_path).suffix.lower().strip(".")
                    supported_exts = ["igs", "iges", "step", "stp", "stl", "obj", "3mf"]

                    if file_ext not in supported_exts:
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
                        file_ext = Path(inner_msg.storage_path).suffix.lower()
                        (
                            metrics,
                            glb_bytes,
                            thumb_bytes,
                        ) = await self.geometry_processor.analyze_async(
                            file_stream, file_ext
                        )

                        # 4. Upload artifacts
                        glb_path = None
                        thumb_path = None

                        if glb_bytes:
                            glb_path = f"{inner_msg.storage_path}_viewer.glb"
                            await self.upload_artifact(
                                glb_bytes, glb_path, "model/gltf-binary"
                            )

                        if thumb_bytes:
                            thumb_path = f"{inner_msg.storage_path}_thumb.png"
                            await self.upload_artifact(
                                thumb_bytes, thumb_path, "image/png"
                            )

                        # 5. Publish Success
                        success_event = FileAnalyzedEvent(
                            messageId=uuid4(),
                            correlationId=correlation_id,
                            messageType=[
                                "urn:message:Maliev.GeometryService.Api.Events:FileAnalyzedEvent"
                            ],
                            message=FileAnalyzedMessage(
                                fileId=file_id,
                                metrics=metrics,
                                processedAt=datetime.now(timezone.utc),
                                glbStoragePath=glb_path,
                                thumbnailStoragePath=thumb_path,
                            ),
                        )
                        await self.publish_event(
                            success_event,
                            "maliev.geometryservice.v1.analysis.completed",
                        )
                        logger.info(
                            "Successfully analyzed file",
                            extra={
                                "file.id": str(file_id),
                                "volume_cm3": metrics.volume_cm3,
                                "surface_area_cm2": metrics.surface_area_cm2,
                                "glb_path": glb_path,
                                "thumb_path": thumb_path,
                            },
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

    async def download_with_retry(
        self, url: str, attempts: int = 3
    ) -> io.BytesIO | None:
        """Implements 3-attempt retry logic with exponential backoff."""
        from src.infrastructure.storage import PermanentDownloadError

        for i in range(attempts):
            try:
                return await self.storage_service.download_file(url)
            except PermanentDownloadError:
                # Do not retry 404, 401, etc.
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
        return None

    async def upload_artifact(self, data: bytes, path: str, content_type: str) -> None:
        """Uploads an artifact (GLB/PNG) to storage."""
        try:
            await self.storage_service.upload_file(data, path, content_type)
        except Exception as e:
            logger.error(f"Failed to upload artifact {path}: {e}")
            # Log warning and continue - user won't see thumbnail but
            # analysis event won't fail
            logger.warning(f"Proceeding without artifact {path}")

    async def publish_failure(
        self, correlation_id: UUID | None, file_id: str, error_code: str, details: str
    ) -> None:
        failure_event = FileAnalysisFailedEvent(
            messageId=uuid4(),
            correlationId=correlation_id,
            messageType=[
                "urn:message:Maliev.GeometryService.Api.Events:FileAnalysisFailedEvent"
            ],
            message=FileAnalysisFailedMessage(
                fileId=file_id, errorCode=error_code, details=details
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
