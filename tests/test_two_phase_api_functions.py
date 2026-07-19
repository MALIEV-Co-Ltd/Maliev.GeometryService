"""Tests for two-phase DFM analysis API functions.

Tests the new API functions for lazy evaluation without requiring full FastAPI setup.
"""

import asyncio
import base64
import json
import time
from contextlib import suppress
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from src.core.geometry_optimizations import clear_cache
from src.main import (
    _file_analysis_cache,
    _resolve_process_dfm_timeout_seconds,
    analyze_for_process,
    cleanup_upload,
    quality_check,
)


@pytest.fixture
def test_assets_dir():
    """Get test assets directory."""
    return Path(__file__).parent / "assets"


@pytest.fixture
def sample_stl_file(test_assets_dir):
    """Get path to a sample STL file."""
    stl_file = test_assets_dir / "50x50x50mm-solid-cube-binary.stl"
    if not stl_file.exists():
        pytest.skip("50x50x50mm-solid-cube-binary.stl not found")
    return stl_file


def _successful_fdm_report() -> dict[str, object]:
    return {
        "reportType": "FDM",
        "thinWallCount": 0,
        "thinWallRegions": [],
        "overhangFaceCount": 0,
        "overhangAreaCm2": 0.0,
        "overhangRegions": [],
        "supportRequired": False,
        "estimatedSupportVolumeCm3": 0.0,
        "smallDetailCount": 0,
        "issues": [],
    }


@pytest.fixture
def sample_step_file(test_assets_dir):
    """Get path to a sample STEP file."""
    step_file = test_assets_dir / "50x50x50mm-solid-cube.step"
    if not step_file.exists():
        pytest.skip("50x50x50mm-solid-cube.step not found")
    return step_file


class TestQualityCheckAPI:
    """Test quality check API function."""

    @pytest.mark.anyio
    async def test_quality_check_completes_quickly(self, sample_stl_file):
        """Test that quality check completes quickly."""
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        start_time = time.time()
        response = await quality_check("test-upload-001", {"stl_bytes": stl_b64})
        duration = time.time() - start_time

        print(f"\nQuality check API call completed in {duration:.2f}s")

        # Should complete quickly
        assert duration < 10.0, f"Quality check too slow: {duration:.2f}s"

        # Check response structure
        assert response.status_code == 200
        data = response.body.decode()
        assert "quality_check_complete" in data

    @pytest.mark.anyio
    async def test_quality_check_caches_file_data(self, sample_stl_file):
        """Test that quality check stores file data for Phase 2."""
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        upload_id = "test-upload-cache"
        await quality_check(upload_id, {"stl_bytes": stl_b64})

        # Verify data is cached
        assert upload_id in _file_analysis_cache
        assert _file_analysis_cache[upload_id]["stl_bytes"] == stl_bytes

        # Clean up
        del _file_analysis_cache[upload_id]


class TestProcessAnalysisAPI:
    """Test process-specific analysis API function."""

    def test_process_analysis_default_timeout_allows_production_step_files(self):
        """Default process DFM timeout must cover larger STEP analysis runs."""
        from src.core.config import settings

        assert settings.GEOMETRY_PROCESS_DFM_TIMEOUT_SECONDS >= 120
        assert (
            _resolve_process_dfm_timeout_seconds(None)
            == settings.GEOMETRY_PROCESS_DFM_TIMEOUT_SECONDS
        )
        assert _resolve_process_dfm_timeout_seconds(5) == 5.0

    @pytest.mark.anyio
    async def test_single_process_analysis(self, sample_stl_file):
        """Test single process analysis after quality check."""
        # First, run quality check
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        await quality_check("test-upload-002", {"stl_bytes": stl_b64})

        # Then, run FDM analysis
        start_time = time.time()
        response = await analyze_for_process(
            "test-upload-002",
            "FDM",
            {"storage_path": "projects/test/test-upload-002/part.stl"},
            timeout=30,
        )
        duration = time.time() - start_time

        print(f"\nFDM analysis API call completed in {duration:.2f}s")

        # Should complete in reasonable time
        assert duration < 30.0, f"FDM analysis too slow: {duration:.2f}s"

        # Should return success
        assert response.status_code == 200
        assert response.headers["x-maliev-geometry-execution-mode"] == (
            "server_fallback_final_validation"
        )
        assert response.headers["x-maliev-geometry-authority"] == "server_authoritative"
        assert response.headers["x-maliev-geometry-server-role"] == (
            "fallback_and_final_validation"
        )
        assert response.headers["x-maliev-geometry-process-code"] == "FDM"
        data = json.loads(response.body.decode())
        assert (
            response.headers["x-maliev-geometry-cache-status"] == data["cache_status"]
        )
        assert data["status"] == "analysis_complete"
        assert data["process_code"] == "FDM"

    @pytest.mark.anyio
    async def test_process_analysis_requires_quality_check_first(self):
        """Test that process analysis fails without quality check."""
        # Try to run process analysis without quality check
        response = await analyze_for_process("test-upload-no-quality", "FDM", timeout=5)

        # Should return 404 - upload not found
        assert response.status_code == 404
        assert response.headers["x-maliev-geometry-execution-mode"] == (
            "server_fallback_final_validation"
        )
        assert response.headers["x-maliev-geometry-authority"] == "server_authoritative"
        assert response.headers["x-maliev-geometry-server-role"] == (
            "fallback_and_final_validation"
        )
        assert response.headers["x-maliev-geometry-process-code"] == "FDM"
        data = response.body.decode()
        assert "NotFound" in data

    @pytest.mark.anyio
    async def test_process_analysis_timeout_publishes_terminal_dfm_event(
        self, sample_stl_file, monkeypatch
    ):
        """Timeouts must still publish a terminal DFM payload for ProjectNew."""
        from src import main

        clear_cache()
        upload_id = str(uuid4())
        _file_analysis_cache[upload_id] = {
            "stl_bytes": sample_stl_file.read_bytes(),
            "storage_path": f"projects/test/{upload_id}/part.stl",
            "body_count": 1,
        }
        published_events = []

        def slow_analysis(*_args, **_kwargs):
            time.sleep(0.05)
            return {"reportType": "CNC_MILL", "issues": []}

        async def capture_event(event, _routing_key):
            published_events.append(event)

        monkeypatch.setattr("src.core.geometry._analyze_single_process", slow_analysis)
        monkeypatch.setattr(main, "publish_event", capture_event)

        try:
            response = await analyze_for_process(upload_id, "CNC_MILL", timeout=0.001)

            assert response.status_code == 504
            assert published_events, "Expected timeout to publish a terminal DFM event"
            payload = published_events[0].message.payload
            assert payload.cnc_report is not None
            assert payload.cnc_report.issues[0].category == "system"
            assert payload.cnc_report.issues[0].severity == "error"
        finally:
            clear_cache()
            with suppress(KeyError):
                del _file_analysis_cache[upload_id]

    @pytest.mark.anyio
    async def test_process_analysis_failure_publishes_terminal_dfm_event(
        self, sample_stl_file, monkeypatch
    ):
        """Analyzer failures must publish a terminal DFM payload for ProjectNew."""
        from src import main

        clear_cache()
        upload_id = "legacy-upload-id"
        storage_path = f"projects/test/{upload_id}/part.stl"
        _file_analysis_cache[upload_id] = {
            "stl_bytes": sample_stl_file.read_bytes(),
            "storage_path": storage_path,
            "body_count": 1,
        }
        published_events = []

        def failed_analysis(*_args, **_kwargs):
            return {
                "error_type": "AnalyzerFailed",
                "message": "Could not analyze printable features.",
            }

        async def capture_event(event, routing_key):
            published_events.append((event, routing_key))

        monkeypatch.setattr(
            "src.core.geometry._analyze_single_process", failed_analysis
        )
        monkeypatch.setattr(main, "publish_event", capture_event)

        try:
            response = await analyze_for_process(upload_id, "FDM", timeout=5)

            assert response.status_code == 500
            data = json.loads(response.body.decode())
            assert data["status"] == "error"
            assert data["dfm_report"]["issues"][0]["category"] == "system"
            assert data["dfm_report"]["issues"][0]["severity"] == "error"
            assert published_events, "Expected failure to publish a terminal DFM event"
            event, routing_key = published_events[0]
            assert routing_key == "maliev.geometryservice.v1.dfm.ready"
            assert event.message.consumed_by == ["IntranetBff", "QuoteEngineBff"]
            assert event.correlation_id == event.message.correlation_id
            assert isinstance(event.correlation_id, UUID)
            assert event.correlation_id == uuid5(
                NAMESPACE_URL,
                f"maliev.geometry:{upload_id}\n{storage_path}",
            )
            payload = event.message.payload
            assert payload.file_id == upload_id
            assert payload.storage_path == storage_path
            assert payload.overlay_paths is None
            serialized = event.model_dump(by_alias=True)
            assert serialized["message"]["payload"]["overlayPaths"] is None
            assert payload.fdm_report is not None
            assert payload.fdm_report.issues[0].category == "system"
            assert payload.fdm_report.issues[0].severity == "error"
        finally:
            clear_cache()
            with suppress(KeyError):
                del _file_analysis_cache[upload_id]

    @pytest.mark.anyio
    async def test_process_analysis_without_storage_path_does_not_publish_dfm_event(
        self,
        monkeypatch,
    ):
        """DFM results without a canonical storage join key must fail closed."""
        from src import main

        clear_cache()
        upload_id = "legacy-upload-without-storage"
        _file_analysis_cache[upload_id] = {
            "stl_bytes": b"fake-stl",
            "body_count": 1,
        }
        published_events = []
        analyzer_calls = []

        def failed_analysis(*_args, **_kwargs):
            analyzer_calls.append(True)
            return {
                "error_type": "AnalyzerFailed",
                "message": "Could not analyze printable features.",
            }

        async def capture_event(event, routing_key):
            published_events.append((event, routing_key))

        monkeypatch.setattr(
            "src.core.geometry._analyze_single_process",
            failed_analysis,
        )
        monkeypatch.setattr(main, "publish_event", capture_event)

        try:
            response = await analyze_for_process(upload_id, "FDM", timeout=5)

            assert response.status_code == 422
            assert analyzer_calls == []
            assert published_events == []
        finally:
            clear_cache()
            with suppress(KeyError):
                del _file_analysis_cache[upload_id]

    @pytest.mark.anyio
    async def test_missing_storage_path_fails_before_analysis_or_publication(
        self,
        monkeypatch,
    ):
        """Canonical storage identity is validated before spending DFM compute."""
        from src import main

        clear_cache()
        upload_id = "legacy-success-without-storage"
        _file_analysis_cache[upload_id] = {
            "stl_bytes": b"successful-fdm-input",
            "body_count": 1,
        }
        published_events = []
        analyzer_calls = []

        async def capture_event(event, routing_key):
            published_events.append((event, routing_key))

        def capture_analysis(*_args, **_kwargs):
            analyzer_calls.append(True)
            return _successful_fdm_report()

        monkeypatch.setattr(
            "src.core.geometry._analyze_single_process",
            capture_analysis,
        )
        monkeypatch.setattr(main, "publish_event", capture_event)

        try:
            response = await analyze_for_process(upload_id, "FDM", timeout=5)
            data = json.loads(response.body.decode())

            assert response.status_code == 422
            assert data["status"] == "invalid_state"
            assert data["error_type"] == "MissingStoragePath"
            assert analyzer_calls == []
            assert published_events == []
        finally:
            clear_cache()
            with suppress(KeyError):
                del _file_analysis_cache[upload_id]

    @pytest.mark.anyio
    async def test_successful_analysis_publisher_failure_returns_handoff_failure(
        self,
        monkeypatch,
    ):
        """Broker failure must not be reported as a completed DFM workflow."""
        from src import main

        clear_cache()
        upload_id = "legacy-success-publisher-failure"
        storage_path = f"projects/test/{upload_id}/part.stl"
        _file_analysis_cache[upload_id] = {
            "stl_bytes": b"successful-fdm-input",
            "storage_path": storage_path,
            "body_count": 1,
        }

        async def fail_publish(_event, _routing_key):
            raise RuntimeError("broker diagnostic must not leak")

        monkeypatch.setattr(
            "src.core.geometry._analyze_single_process",
            lambda *_args, **_kwargs: _successful_fdm_report(),
        )
        monkeypatch.setattr(main, "publish_event", fail_publish)

        try:
            response = await analyze_for_process(upload_id, "FDM", timeout=5)
            data = json.loads(response.body.decode())

            assert response.status_code == 503
            assert data["status"] == "handoff_failed"
            assert data["error_type"] == "EventPublicationFailed"
            assert "broker diagnostic" not in response.body.decode()
        finally:
            clear_cache()
            with suppress(KeyError):
                del _file_analysis_cache[upload_id]

    @pytest.mark.anyio
    async def test_successful_analysis_publication_cancellation_propagates(
        self,
        monkeypatch,
    ):
        """Task cancellation is control flow and must never become a 503 response."""
        from src import main

        clear_cache()
        upload_id = "legacy-success-publication-cancelled"
        _file_analysis_cache[upload_id] = {
            "stl_bytes": b"successful-fdm-input",
            "storage_path": f"projects/test/{upload_id}/part.stl",
            "body_count": 1,
        }

        async def cancel_publish(_event, _routing_key):
            raise asyncio.CancelledError

        monkeypatch.setattr(
            "src.core.geometry._analyze_single_process",
            lambda *_args, **_kwargs: _successful_fdm_report(),
        )
        monkeypatch.setattr(main, "publish_event", cancel_publish)

        try:
            with pytest.raises(asyncio.CancelledError):
                await analyze_for_process(upload_id, "FDM", timeout=5)
        finally:
            clear_cache()
            with suppress(KeyError):
                del _file_analysis_cache[upload_id]


class TestCleanupAPI:
    """Test cleanup API function."""

    @pytest.mark.anyio
    async def test_cleanup_upload(self, sample_stl_file):
        """Test cleaning up upload data."""
        # First, create an upload
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        await quality_check("test-upload-cleanup", {"stl_bytes": stl_b64})

        # Clean it up
        response = await cleanup_upload("test-upload-cleanup")

        assert response.status_code == 200
        assert "test-upload-cleanup" not in _file_analysis_cache

    @pytest.mark.anyio
    async def test_cleanup_nonexistent_upload(self):
        """Test cleaning up upload that doesn't exist."""
        response = await cleanup_upload("test-upload-nonexistent")
        assert response.status_code == 404


class TestEndToEndWorkflow:
    """Test complete two-phase workflow."""

    @pytest.mark.anyio
    async def test_complete_two_phase_workflow(self, sample_stl_file):
        """Test the complete two-phase workflow as it would be used in production."""
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")
        upload_id = "test-upload-e2e"

        # Phase 1: Quality check (fast, shows preview)
        start_time = time.time()
        quality_response = await quality_check(upload_id, {"stl_bytes": stl_b64})
        quality_duration = time.time() - start_time

        assert quality_response.status_code == 200
        quality_data = quality_response.body.decode()
        assert "quality_check_complete" in quality_data

        print(f"\nPhase 1 (Quality Check): {quality_duration:.2f}s")

        # Phase 2a: User selects FDM - analyze only FDM
        start_time = time.time()
        request = {"storage_path": f"projects/test/{upload_id}/part.stl"}
        fdm_response = await analyze_for_process(
            upload_id,
            "FDM",
            request,
            timeout=30,
        )
        fdm_duration = time.time() - start_time

        assert fdm_response.status_code == 200
        fdm_data = fdm_response.body.decode()
        assert "analysis_complete" in fdm_data

        print(f"Phase 2a (FDM Analysis): {fdm_duration:.2f}s")

        # Phase 2b: User changes mind to CNC - analyze only CNC
        start_time = time.time()
        cnc_response = await analyze_for_process(
            upload_id,
            "CNC_MILL",
            request,
            timeout=30,
        )
        cnc_duration = time.time() - start_time

        assert cnc_response.status_code == 200
        cnc_data = cnc_response.body.decode()
        assert "analysis_complete" in cnc_data

        print(f"Phase 2b (CNC Analysis): {cnc_duration:.2f}s")

        # Phase 3: Cleanup
        await cleanup_upload(upload_id)

        total_time = quality_duration + fdm_duration + cnc_duration
        print(f"Total two-phase workflow: {total_time:.2f}s")

        # Total should be much faster than old approach (90+ seconds)
        assert total_time < 60.0, f"Two-phase workflow too slow: {total_time:.2f}s"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
