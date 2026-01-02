import asyncio
import io
import json
import logging
import os
from datetime import UTC, datetime
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
        self.connection = await aio_pika.connect_robust(settings.RABBITMQ_URI)
        self.channel = await self.connection.channel()
        await self.channel.set_qos(prefetch_count=1)

        # Declare queues/exchanges
        self.queue = cast(
            aio_pika.abc.AbstractRobustQueue,
            await self.channel.declare_queue("geometry-analysis-queue", durable=True),
        )
        self.exchange = cast(
            aio_pika.abc.AbstractRobustExchange,
            await self.channel.declare_exchange(
                "maliev.events", type="topic", durable=True
            ),
        )

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
                try:
                    body = json.loads(message.body.decode())
                    event = FileUploadedEvent.model_validate(body)
                    inner_msg = event.message

                    file_id = inner_msg.file_id or inner_msg.upload_id
                    span.set_attribute("file_id", str(file_id))
                    logger.info(f"Processing file {file_id}")

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
                        metrics = await self.geometry_processor.analyze_async(
                            file_stream, file_ext
                        )

                        # 4. Publish Success
                        success_event = FileAnalyzedEvent(
                            messageId=uuid4(),
                            correlationId=event.correlation_id,
                            messageType=[
                                "urn:message:Maliev.GeometryService.Api.Events:FileAnalyzedEvent"
                            ],
                            message=FileAnalyzedMessage(
                                fileId=file_id,
                                metrics=metrics,
                                processedAt=datetime.now(UTC),
                            ),
                        )
                        await self.publish_event(
                            success_event,
                            "maliev.geometryservice.v1.analysis.completed",
                        )
                        logger.info(f"Successfully analyzed file {file_id}")

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
                        event.correlation_id, file_id, error_code, str(e)
                    )
                except Exception as e:
                    logger.error(f"Error processing {file_id}: {e}")
                    await self.publish_failure(
                        event.correlation_id, file_id, "SYSTEM_ERROR", str(e)
                    )

    async def download_with_retry(
        self, url: str, attempts: int = 3
    ) -> io.BytesIO | None:
        """Implements 3-attempt retry logic with exponential backoff."""
        for i in range(attempts):
            try:
                return await self.storage_service.download_file(url)
            except Exception as e:
                if i == attempts - 1:
                    raise e
                wait_time = 2 ** (i + 1)
                logger.warning(
                    f"Download failed, retrying in {wait_time}s... "
                    f"(Attempt {i+1}/{attempts})"
                )
                await asyncio.sleep(wait_time)
        return None

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
