import asyncio
import contextlib
import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import respx

from src.consumers.upload_consumer import (
    ArtifactProcessingJob,
    UploadConsumer,
    _browser_viewer_source_extension,
)
from src.core.geometry import BoundingBox, GeometryMetrics
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
    "cad_glb_bytes": b"fake-glb",
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
async def test_validate_file_size_before_download_normalizes_mock_storage_url(
    consumer, monkeypatch
):
    monkeypatch.setattr(
        "src.infrastructure.storage.settings.UPLOAD_SERVICE_URL",
        "http://aspire.dev.internal:45389",
    )
    original_url = "http://localhost:55333/upload/v1/mock-storage/token-123"
    normalized_url = "http://aspire.dev.internal:45389/upload/v1/mock-storage/token-123"

    with respx.mock:
        route = respx.head(normalized_url).mock(
            return_value=httpx.Response(200, headers={"Content-Length": "1024"})
        )

        assert await consumer.validate_file_size_before_download(original_url)
        assert route.called

    await consumer.stop()


class _TrackedMessageProcess:
    def __init__(self, events: list[str]):
        self._events = events

    async def __aenter__(self):
        self._events.append("ack-enter")

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        self._events.append("ack-exit")
        return False


def _build_upload_message(file_name: str = "test.stl") -> MagicMock:
    inner_msg = UploadCompletedMessage(
        uploadId=str(uuid4()),
        fileId=str(uuid4()),
        serviceId="test-service",
        fileName=file_name,
        storagePath=f"test/{file_name}",
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
        message_type=[
            "urn:message:Maliev.UploadService.Api.Events:UploadCompletedEvent"
        ],
    )
    message = MagicMock()
    message.body = event.model_dump_json(by_alias=True).encode()
    return message


def test_rabbitmq_prefetch_defaults_to_ingest_concurrency(
    consumer: UploadConsumer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert consumer._settings.GEOMETRY_FILE_INGEST_CONCURRENCY == 2
    monkeypatch.setattr(
        "src.consumers.upload_consumer.settings.GEOMETRY_RABBITMQ_PREFETCH",
        None,
    )

    assert consumer._rabbitmq_prefetch_count() == 2


@pytest.mark.asyncio
async def test_process_message_acks_after_metrics_without_waiting_for_artifacts(
    consumer: UploadConsumer,
    mock_storage: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    message = _build_upload_message()
    message.process.return_value = _TrackedMessageProcess(events)
    mock_storage.download_file.return_value = io.BytesIO(b"fake-stl-content")
    consumer.publish_event = AsyncMock()
    consumer.validate_file_size_before_download = AsyncMock(return_value=True)

    loop = asyncio.get_event_loop()
    run_in_executor_call_count = 0

    async def _never_finishes() -> None:
        await asyncio.Event().wait()

    def fake_run_in_executor(executor, fn, *args):  # noqa: ANN001, ARG001
        nonlocal run_in_executor_call_count
        run_in_executor_call_count += 1
        if run_in_executor_call_count == 1:
            future = loop.create_future()
            future.set_result(_FAKE_METRICS_RESULT)
            return future
        return asyncio.ensure_future(_never_finishes())

    mock_loop = MagicMock(wraps=loop)
    mock_loop.run_in_executor = fake_run_in_executor

    artifact_release = asyncio.Event()

    async def fake_run_artifact_job(job):  # noqa: ANN001, ARG001
        events.append("artifact-start")
        await artifact_release.wait()
        events.append("artifact-end")

    monkeypatch.setattr(
        consumer,
        "_run_artifact_job",
        fake_run_artifact_job,
        raising=False,
    )

    with patch(
        "src.consumers.upload_consumer.asyncio.get_running_loop",
        return_value=mock_loop,
    ):
        task = asyncio.create_task(consumer.process_message(message))
        done, pending = await asyncio.wait({task}, timeout=0.2)

    if pending:
        artifact_release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert task in done
    assert "ack-exit" in events
    assert "artifact-end" not in events

    artifact_release.set()
    if hasattr(consumer, "wait_for_artifact_tasks"):
        await consumer.wait_for_artifact_tasks()


@pytest.mark.asyncio
async def test_artifact_job_waits_for_viewer_glb_before_secondary_artifacts(
    consumer: UploadConsumer,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    glb_started = asyncio.Event()
    allow_glb_to_finish = asyncio.Event()

    artifact_temp_dir = tmp_path / "artifact"
    artifact_temp_dir.mkdir()
    job = ArtifactProcessingJob(
        file_id=str(uuid4()),
        upload_id=str(uuid4()),
        storage_path="projects/test/model.step",
        file_ext=".step",
        file_name="model.step",
        file_size=1024,
        correlation_id=uuid4(),
        metrics=MagicMock(),
        body_count=1,
        body_infos=None,
        temp_dir=artifact_temp_dir,
        cad_glb_path=artifact_temp_dir / "cad.glb",
        executor=MagicMock(),
        queued_at=0.0,
    )

    async def fake_publish_glb(job: ArtifactProcessingJob) -> bool:  # noqa: ARG001
        events.append("glb-start")
        glb_started.set()
        await allow_glb_to_finish.wait()
        events.append("glb-end")
        return True

    async def fake_publish_small_thumbnail(
        job: ArtifactProcessingJob,  # noqa: ARG001
    ) -> bool:
        events.append("thumb-start")
        return True

    async def fake_publish_previews(job: ArtifactProcessingJob) -> bool:  # noqa: ARG001
        events.append("previews-start")
        return True

    monkeypatch.setattr(consumer, "_publish_glb", fake_publish_glb)
    monkeypatch.setattr(
        consumer, "_publish_small_thumbnail", fake_publish_small_thumbnail
    )
    monkeypatch.setattr(consumer, "_publish_previews", fake_publish_previews)
    consumer.publish_failure = AsyncMock()

    task = asyncio.create_task(consumer._run_artifact_job(job))
    await asyncio.wait_for(glb_started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert events == ["glb-start"]

    allow_glb_to_finish.set()
    await task

    assert events.index("thumb-start") > events.index("glb-end")
    assert events.index("previews-start") > events.index("glb-end")
    consumer.publish_failure.assert_not_called()


def test_browser_viewer_source_extension_allows_only_self_contained_meshes() -> None:
    assert _browser_viewer_source_extension("stl") == ".stl"
    assert _browser_viewer_source_extension(".OBJ") == ".obj"
    assert _browser_viewer_source_extension(".glb") == ".glb"
    assert _browser_viewer_source_extension(".step") is None
    assert _browser_viewer_source_extension(".gltf") is None
    assert _browser_viewer_source_extension(".3mf") is None


@pytest.mark.asyncio
async def test_artifact_job_skips_glb_export_for_browser_loadable_mesh(
    consumer: UploadConsumer,
    tmp_path,
) -> None:
    artifact_temp_dir = tmp_path / "artifact"
    artifact_temp_dir.mkdir()
    job = ArtifactProcessingJob(
        file_id=str(uuid4()),
        upload_id=str(uuid4()),
        storage_path="projects/test/model.stl",
        file_ext=".stl",
        file_name="model.stl",
        file_size=1024,
        correlation_id=uuid4(),
        metrics=GeometryMetrics(
            volumeCm3=1.0,
            supportVolumeCm3=0.0,
            surfaceAreaCm2=6.0,
            boundingBox=BoundingBox(x=1.0, y=1.0, z=1.0),
            isManifold=True,
            triangleCount=12,
            eulerNumber=2,
        ),
        body_count=1,
        body_infos=None,
        temp_dir=artifact_temp_dir,
        cad_glb_path=artifact_temp_dir / "cad.glb",
        executor=None,
        queued_at=0.0,
    )
    consumer._publish_glb = AsyncMock(return_value=False)  # type: ignore[method-assign]
    consumer._publish_small_thumbnail = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    consumer._publish_previews = AsyncMock(return_value=True)  # type: ignore[method-assign]
    consumer.publish_failure = AsyncMock()
    consumer.publish_event = AsyncMock()

    await consumer._run_artifact_job(job)

    consumer._publish_glb.assert_not_awaited()
    consumer.publish_failure.assert_not_awaited()
    consumer.publish_event.assert_awaited_once()
    completed_event = consumer.publish_event.await_args.args[0]
    assert completed_event.message.payload.glb_storage_path is None
    assert completed_event.message.payload.viewer_storage_path == (
        "projects/test/model.stl"
    )
    assert completed_event.message.payload.viewer_file_extension == ".stl"


@pytest.mark.asyncio
async def test_publish_glb_timeout_uses_source_glb_fallback(
    consumer: UploadConsumer,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_glb = b"source-glb"
    artifact_temp_dir = tmp_path / "artifact"
    artifact_temp_dir.mkdir()
    cad_glb_path = artifact_temp_dir / "cad.glb"
    cad_glb_path.write_bytes(source_glb)
    job = ArtifactProcessingJob(
        file_id=str(uuid4()),
        upload_id=str(uuid4()),
        storage_path="projects/test/model.step",
        file_ext=".step",
        file_name="model.step",
        file_size=1024,
        correlation_id=uuid4(),
        metrics=GeometryMetrics(
            volumeCm3=1.0,
            supportVolumeCm3=0.0,
            surfaceAreaCm2=6.0,
            boundingBox=BoundingBox(x=1.0, y=1.0, z=1.0),
            isManifold=True,
            triangleCount=12,
            eulerNumber=2,
        ),
        body_count=1,
        body_infos=None,
        temp_dir=artifact_temp_dir,
        cad_glb_path=cad_glb_path,
        executor=None,
        queued_at=0.0,
    )
    consumer.upload_artifact = AsyncMock(return_value=True)
    consumer.publish_event = AsyncMock()

    async def timeout_wait_for(awaitable, timeout):  # noqa: ANN001, ARG001
        awaitable.cancel()
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        "src.consumers.upload_consumer.asyncio.wait_for",
        timeout_wait_for,
    )

    result = await consumer._publish_glb(job)

    assert result is True
    consumer.upload_artifact.assert_awaited_once_with(
        source_glb,
        "projects/test/model.step_viewer.glb",
        "model/gltf-binary",
        job.upload_id,
    )
    completed_event = consumer.publish_event.await_args.args[0]
    assert consumer.publish_event.await_args.args[1] == (
        "maliev.geometryservice.v1.analysis.completed"
    )
    assert completed_event.message.payload.file_id == job.file_id
    assert completed_event.message.payload.glb_storage_path == (
        "projects/test/model.step_viewer.glb"
    )
    assert completed_event.message.payload.viewer_storage_path == (
        "projects/test/model.step_viewer.glb"
    )
    assert completed_event.message.payload.viewer_file_extension == ".glb"



@pytest.mark.asyncio
async def test_publish_previews_includes_small_thumbnail_path(
    consumer: UploadConsumer,
    tmp_path,
) -> None:
    artifact_temp_dir = tmp_path / "artifact"
    artifact_temp_dir.mkdir()
    cad_glb_path = artifact_temp_dir / "cad.glb"
    cad_glb_path.write_bytes(b"source-glb")
    job = ArtifactProcessingJob(
        file_id=str(uuid4()),
        upload_id=str(uuid4()),
        storage_path="projects/test/model.step",
        file_ext=".step",
        file_name="model.step",
        file_size=1024,
        correlation_id=uuid4(),
        metrics=MagicMock(),
        body_count=1,
        body_infos=None,
        temp_dir=artifact_temp_dir,
        cad_glb_path=cad_glb_path,
        executor=None,
        queued_at=0.0,
    )
    consumer.upload_artifact = AsyncMock(return_value=True)
    consumer.publish_event = AsyncMock()
    real_loop = asyncio.get_event_loop()

    def fake_run_in_executor(executor, fn, *args):  # noqa: ANN001, ARG001
        assert fn.__name__ == "_render_preview_worker"
        future = real_loop.create_future()
        future.set_result(_FAKE_ARTIFACTS_RESULT["preview_images"])
        return future

    mock_loop = MagicMock(wraps=real_loop)
    mock_loop.run_in_executor = fake_run_in_executor

    with patch(
        "src.consumers.upload_consumer.asyncio.get_running_loop",
        return_value=mock_loop,
    ):
        result = await consumer._publish_previews(job)

    assert result is True
    uploaded_paths = [call.args[1] for call in consumer.upload_artifact.await_args_list]
    assert "projects/test/model.step_thumbnail_small.webp" in uploaded_paths

    preview_event = consumer.publish_event.await_args.args[0]
    preview_images = preview_event.message.payload.preview_images
    assert preview_images.thumbnail_small == (
        "projects/test/model.step_thumbnail_small.webp"
    )
    assert preview_images.thumbnail_large == (
        "projects/test/model.step_thumbnail_large.webp"
    )


@pytest.mark.asyncio
async def test_process_message_success(consumer, mock_storage, mock_processor):  # noqa: ARG001
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
    consumer.upload_artifact = AsyncMock(return_value=True)
    consumer.validate_file_size_before_download = AsyncMock(return_value=True)
    consumer._token_provider.get_token = MagicMock(return_value="fake-jwt-token")

    # Build a mock event loop that returns predetermined results from run_in_executor.
    # Phase 1 returns metrics dict; Phase 2 returns artifacts dict.
    run_in_executor_call_count = 0

    real_loop = asyncio.get_event_loop()

    async def _coro_phase1():
        return _FAKE_METRICS_RESULT

    def fake_run_in_executor(executor, fn, *args):  # noqa: ARG001
        nonlocal run_in_executor_call_count
        run_in_executor_call_count += 1
        if fn.__name__ == "_compute_metrics_worker":
            return asyncio.ensure_future(_coro_phase1())
        if fn.__name__ == "_render_thumbnail_worker":
            future = real_loop.create_future()
            future.set_result(b"thumb-small")
            return future
        if fn.__name__ == "_export_glb_from_paths":
            future = real_loop.create_future()
            future.set_result(b"glb-content")
            return future
        if fn.__name__ == "_render_preview_worker":
            future = real_loop.create_future()
            future.set_result(_FAKE_ARTIFACTS_RESULT["preview_images"])
            return future
        raise AssertionError(f"Unexpected executor function: {fn.__name__}")

    mock_loop = MagicMock(wraps=real_loop)
    mock_loop.run_in_executor = fake_run_in_executor

    with patch(
        "src.consumers.upload_consumer.asyncio.get_running_loop", return_value=mock_loop
    ):
        await consumer.process_message(message)
        await consumer.wait_for_artifact_tasks()

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
        c
        for c in consumer.publish_event.call_args_list
        if c[0][1] == "maliev.geometryservice.v1.analysis.completed"
    )
    success_event = completed_call[0][0]
    assert success_event.correlation_id == correlation_id
    assert success_event.message.payload.metrics.volume_cm3 == 1.0


@pytest.mark.asyncio
async def test_process_message_with_deferred_sla_report_does_not_crash(
    consumer,
    mock_storage,
    mock_processor,  # noqa: ARG001
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
    _DEFERRED_METRICS = {  # noqa: N806
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
    consumer.upload_artifact = AsyncMock(return_value=True)
    consumer.validate_file_size_before_download = AsyncMock(return_value=True)
    consumer._token_provider.get_token = MagicMock(return_value="fake-jwt-token")

    run_in_executor_call_count = 0
    real_loop = asyncio.get_event_loop()

    async def _coro_phase1():
        return _DEFERRED_METRICS

    def fake_run_in_executor(executor, fn, *args):  # noqa: ARG001
        nonlocal run_in_executor_call_count
        run_in_executor_call_count += 1
        if fn.__name__ == "_compute_metrics_worker":
            return asyncio.ensure_future(_coro_phase1())
        if fn.__name__ == "_render_thumbnail_worker":
            future = real_loop.create_future()
            future.set_result(b"thumb-small")
            return future
        if fn.__name__ == "_export_glb_from_paths":
            future = real_loop.create_future()
            future.set_result(b"glb-content")
            return future
        if fn.__name__ == "_render_preview_worker":
            future = real_loop.create_future()
            future.set_result(_FAKE_ARTIFACTS_RESULT["preview_images"])
            return future
        raise AssertionError(f"Unexpected executor function: {fn.__name__}")

    mock_loop = MagicMock(wraps=real_loop)
    mock_loop.run_in_executor = fake_run_in_executor

    with patch(
        "src.consumers.upload_consumer.asyncio.get_running_loop", return_value=mock_loop
    ):
        # Must not raise pydantic ValidationError
        await consumer.process_message(message)
        await consumer.wait_for_artifact_tasks()

    # DFM event must NOT be published during upload (lazy DFM)
    routing_keys = [call[0][1] for call in consumer.publish_event.call_args_list]
    assert "maliev.geometryservice.v1.dfm.ready" not in routing_keys

    # analysis.completed must still be published with dfm_report=None
    completed_call = next(
        c
        for c in consumer.publish_event.call_args_list
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
    consumer.validate_file_size_before_download = AsyncMock(return_value=True)
    consumer._token_provider.get_token = MagicMock(return_value="fake-jwt-token")

    # Execute
    await consumer.process_message(message)

    # Assert
    assert consumer.publish_event.called
    routing_key = consumer.publish_event.call_args[0][1]
    assert routing_key == "maliev.geometryservice.v1.analysis.failed"
