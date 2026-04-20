import asyncio
import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.consumers.upload_consumer import UploadConsumer
from src.core.schemas import (
    FileUploadedEvent,
    FileUploadedMessage,
    UploadCompletedMessage,
)

# Fake metrics dict returned by _compute_metrics_worker in the process pool
_FAKE_METRICS_RESULT = {
    "volume_cm3": 1.0,
    "support_volume_cm3": 0.5,
    "surface_area_cm2": 6.0,
    "bounding_box": {"x": 10.0, "y": 10.0, "z": 10.0},
    "is_manifold": True,
    "triangle_count": 12,
    "euler_number": 2,
    "mesh_stl_bytes": b"fake-stl",
    "dfmReports": {},
}

# Fake artifacts dict returned by _generate_artifacts_worker
_FAKE_ARTIFACTS_RESULT = {
    "glb_bytes": b"glb-content",
    "thumbnail_bytes": b"thumb-content",
    "preview_images": {
        "front_small": b"front-png",
        "back_small": b"back-png",
        "left_small": b"left-png",
        "right_small": b"right-png",
        "top_small": b"top-png",
        "bottom_small": b"bottom-png",
        "thumbnail_small": b"thumb-small",
        "thumbnail_large": b"thumb-large",
    },
}


@pytest.fixture
def mock_storage():
    return AsyncMock()


@pytest.fixture
def mock_processor():
    mock = MagicMock()
    mock.executor = MagicMock()
    return mock


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

    mock_storage.download_file.return_value = io.BytesIO(b"fake-stl-content")
    consumer.publish_event = AsyncMock()
    consumer.upload_artifact = AsyncMock()
    consumer._token_provider.get_token = MagicMock(return_value="fake-jwt-token")

    # Build a mock event loop that returns predetermined results from run_in_executor.
    # Phase 1 returns metrics dict; Phase 2 returns artifacts dict.
    run_in_executor_call_count = 0

    real_loop = asyncio.get_event_loop()

    async def _coro_phase1():
        return _FAKE_METRICS_RESULT

    async def _coro_phase2():
        return _FAKE_ARTIFACTS_RESULT

    def fake_run_in_executor(executor, fn, *args):
        nonlocal run_in_executor_call_count
        run_in_executor_call_count += 1
        if run_in_executor_call_count == 1:
            return asyncio.ensure_future(_coro_phase1())
        return asyncio.ensure_future(_coro_phase2())

    mock_loop = MagicMock(wraps=real_loop)
    mock_loop.run_in_executor = fake_run_in_executor

    with patch("src.consumers.upload_consumer.asyncio.get_running_loop", return_value=mock_loop):
        await consumer.process_message(message)

    # Assert at least one publish_event call happened
    assert consumer.publish_event.called

    routing_keys = [call[0][1] for call in consumer.publish_event.call_args_list]

    # metrics.ready must be published (Phase 1)
    assert "maliev.geometryservice.v1.metrics.ready" in routing_keys
    # dfm.ready must NOT be published during upload (lazy DFM)
    assert "maliev.geometryservice.v1.dfm.ready" not in routing_keys
    # analysis.completed must be published
    assert "maliev.geometryservice.v1.analysis.completed" in routing_keys

    # Verify correlation_id propagation on the analysis.completed event
    completed_call = next(
        c for c in consumer.publish_event.call_args_list
        if c[0][1] == "maliev.geometryservice.v1.analysis.completed"
    )
    success_event = completed_call[0][0]
    assert success_event.correlation_id == correlation_id
    assert success_event.message.payload.metrics.volume_cm3 == 1.0


@pytest.mark.asyncio
async def test_process_message_with_deferred_sla_report_does_not_crash(
    consumer, mock_storage, mock_processor
):
    """Regression: deferred SLA reports (twoPhaseDeferred=True) must not cause
    pydantic ValidationError in the consumer (missing resinTrappingRisk etc.)."""
    correlation_id = uuid4()
    file_id = str(uuid4())
    upload_id = str(uuid4())

    inner_msg = UploadCompletedMessage(
        uploadId=upload_id,
        fileId=file_id,
        serviceId="test-service",
        fileName="test.obj",
        storagePath="test/test.obj",
        downloadUrl="http://signed-url",
        contentType="model/obj",
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

    # Deferred SLA/FDM reports — missing SLA-specific required fields — the
    # consumer must skip them rather than calling model_validate and crashing.
    _DEFERRED_METRICS = {
        **_FAKE_METRICS_RESULT,
        "dfmReports": {
            "FDM": {
                "reportType": "FDM",
                "thinWallCount": 0,
                "thinWallRegions": [],
                "overhangFaceCount": 0,
                "overhangAreaCm2": 0.0,
                "overhangRegions": [],
                "supportRequired": False,
                "estimatedSupportVolumeCm3": None,
                "smallDetailCount": 0,
                "issues": [],
                "twoPhaseDeferred": True,
            },
            "SLA": {
                "reportType": "SLA",
                "thinWallCount": 0,
                "thinWallRegions": [],
                "overhangFaceCount": 0,
                "overhangAreaCm2": 0.0,
                "overhangRegions": [],
                "supportRequired": False,
                "estimatedSupportVolumeCm3": None,
                "smallDetailCount": 0,
                "issues": [],
                "twoPhaseDeferred": True,
                # NOTE: SLA-specific required fields intentionally omitted here
                # to simulate the pre-fix deferred report. Consumer must skip it.
            },
        },
    }

    message = MagicMock()
    message.body = event.model_dump_json(by_alias=True).encode()
    message.process.return_value.__aenter__ = AsyncMock()

    mock_storage.download_file.return_value = io.BytesIO(b"fake-obj-content")
    consumer.publish_event = AsyncMock()
    consumer.upload_artifact = AsyncMock()
    consumer._token_provider.get_token = MagicMock(return_value="fake-jwt-token")

    run_in_executor_call_count = 0
    real_loop = asyncio.get_event_loop()

    async def _coro_phase1():
        return _DEFERRED_METRICS

    async def _coro_phase2():
        return _FAKE_ARTIFACTS_RESULT

    def fake_run_in_executor(executor, fn, *args):
        nonlocal run_in_executor_call_count
        run_in_executor_call_count += 1
        if run_in_executor_call_count == 1:
            return asyncio.ensure_future(_coro_phase1())
        return asyncio.ensure_future(_coro_phase2())

    mock_loop = MagicMock(wraps=real_loop)
    mock_loop.run_in_executor = fake_run_in_executor

    with patch("src.consumers.upload_consumer.asyncio.get_running_loop", return_value=mock_loop):
        # Must not raise pydantic ValidationError
        await consumer.process_message(message)

    # DFM event must NOT be published during upload (lazy DFM)
    routing_keys = [call[0][1] for call in consumer.publish_event.call_args_list]
    assert "maliev.geometryservice.v1.dfm.ready" not in routing_keys

    # analysis.completed must still be published with dfm_report=None
    completed_call = next(
        c for c in consumer.publish_event.call_args_list
        if c[0][1] == "maliev.geometryservice.v1.analysis.completed"
    )
    completed_event = completed_call[0][0]
    assert completed_event.message.payload.dfm_report is None


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
