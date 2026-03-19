import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.consumers.upload_consumer import UploadConsumer
from src.core.geometry import BoundingBox, GeometryMetrics
from src.core.schemas import (
    FileUploadedEvent,
    FileUploadedMessage,
    UploadCompletedMessage,
)


@pytest.fixture
def mock_storage():
    return AsyncMock()


@pytest.fixture
def mock_processor():
    return AsyncMock()


@pytest.fixture
def consumer(mock_storage, mock_processor):
    return UploadConsumer(mock_storage, mock_processor)


@pytest.mark.asyncio
async def test_process_message_success(consumer, mock_storage, mock_processor):
    # Setup
    correlation_id = uuid4()
    file_id = str(uuid4())
    upload_id = str(uuid4())

    inner_msg = UploadCompletedMessage(
        uploadId=upload_id,
        fileId=file_id,
        serviceId="test-service",
        fileName="test.stl",
        storagePath="test/test.stl",
        downloadUrl="http://signed-url",
        contentType="model/stl",
        fileSize=1024,
        uploadedAt=datetime.now(timezone.utc),
    )

    event = FileUploadedEvent(
        messageId=uuid4(),
        correlationId=correlation_id,
        message=FileUploadedMessage(
            messageId=uuid4(),
            messageName="UploadCompleted",
            payload=inner_msg,
        ),
        message_type=[
            "urn:message:Maliev.UploadService.Api.Events:UploadCompletedEvent"
        ],
    )

    message = MagicMock()
    message.body = event.model_dump_json(by_alias=True).encode()
    message.process.return_value.__aenter__ = AsyncMock()

    mock_storage.download_file.return_value = io.BytesIO(b"content")
    mock_processor.analyze_async.return_value = (
        GeometryMetrics(
            volume_cm3=1.0,
            support_volume_cm3=0.5,
            surface_area_cm2=6.0,
            bounding_box=BoundingBox(x=10, y=10, z=10),
            is_manifold=True,
            triangle_count=12,
            euler_number=2,
        ),
        b"glb-content",
        b"thumb-content",
        {
            "front": b"front-png",
            "back": b"back-png",
            "left": b"left-png",
            "right": b"right-png",
            "top": b"top-png",
            "bottom": b"bottom-png",
        },
    )

    consumer.publish_event = AsyncMock()
    consumer._token_provider.get_token = MagicMock(return_value="fake-jwt-token")

    # Execute
    await consumer.process_message(message)

    # Assert
    assert consumer.publish_event.called
    # Check first call (analysis.completed) - use call_args_list[0] not call_args
    # because preview-images.generated is published after, overwriting call_args
    first_call_args = consumer.publish_event.call_args_list[0]
    routing_key = first_call_args[0][1]
    assert routing_key == "maliev.geometryservice.v1.analysis.completed"
    success_event = first_call_args[0][0]
    assert success_event.correlation_id == correlation_id
    assert success_event.message.metrics.volume_cm3 == 1.0


@pytest.mark.asyncio
async def test_process_message_failure(consumer, mock_storage):
    # Setup
    inner_msg = UploadCompletedMessage(
        uploadId=str(uuid4()),
        serviceId="test-service",
        fileName="test.stl",
        storagePath="test/test.stl",
        downloadUrl="http://signed-url",
        contentType="model/stl",
        fileSize=1024,
        uploadedAt=datetime.now(timezone.utc),
    )
    event = FileUploadedEvent(
        messageId=uuid4(),
        correlationId=uuid4(),
        message=FileUploadedMessage(
            messageId=uuid4(),
            messageName="UploadCompleted",
            payload=inner_msg,
        ),
    )

    message = MagicMock()
    message.body = event.model_dump_json(by_alias=True).encode()
    message.process.return_value.__aenter__ = AsyncMock()

    mock_storage.download_file.side_effect = Exception("Download failed")
    consumer.publish_event = AsyncMock()
    consumer._token_provider.get_token = MagicMock(return_value="fake-jwt-token")

    # Execute
    await consumer.process_message(message)

    # Assert
    assert consumer.publish_event.called
    routing_key = consumer.publish_event.call_args[0][1]
    assert routing_key == "maliev.geometryservice.v1.analysis.failed"
