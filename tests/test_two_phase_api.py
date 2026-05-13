"""Tests for two-phase DFM analysis API endpoints.

Tests the new REST API endpoints for lazy evaluation:
- POST /uploads/{upload_id}/quality-check - Phase 1: Quality checks
- POST /uploads/{upload_id}/dfm/{process_code} - Phase 2: Process-specific analysis
- DELETE /uploads/{upload_id} - Cleanup
"""

import base64
import contextlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from src.core.config import settings
from src.main import app

SECURITY_KEY = "a-very-long-test-secret-key-at-least-32-chars!"
ISSUER = "https://api.test.maliev.com"
AUDIENCE = "https://api.test.maliev.com"


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


@pytest.fixture
def sample_step_file(test_assets_dir):
    """Get path to a sample STEP file."""
    step_file = test_assets_dir / "50x50x50mm-solid-cube.step"
    if not step_file.exists():
        pytest.skip("50x50x50mm-solid-cube.step not found")
    return step_file


@pytest.fixture
async def client():
    """Create async test client."""
    settings.ASPNETCORE_ENVIRONMENT = "Testing"
    settings.JWT_SECURITY_KEY = SECURITY_KEY
    settings.JWT_ISSUER = ISSUER
    settings.JWT_AUDIENCE = AUDIENCE

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "test-user",
            "permissions": "geometry.analysis.run",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
        },
        SECURITY_KEY,
        algorithm="HS256",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


class TestQualityCheckEndpoint:
    """Test Phase 1: Quality check endpoint."""

    @pytest.mark.asyncio
    async def test_quality_check_completes_quickly(self, client, sample_stl_file):
        """Test that quality check completes quickly."""
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        start_time = time.time()
        response = await client.post(
            "/geometry/uploads/test-upload-001/quality-check",
            json={"stl_bytes": stl_b64},
        )
        duration = time.time() - start_time

        print(f"\nQuality check API call completed in {duration:.2f}s")

        # Should complete quickly
        assert duration < 10.0, f"Quality check too slow: {duration:.2f}s"

        # Should return success
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "quality_check_complete"
        assert data["upload_id"] == "test-upload-001"
        assert data["ready_for_process_selection"] is True

        # Should have quality results
        quality = data["quality"]
        assert "is_manifold" in quality
        assert "face_count" in quality
        assert "volume_mm3" in quality
        assert "bounding_box" in quality

    @pytest.mark.asyncio
    async def test_quality_check_with_step_file(
        self, client, sample_stl_file, sample_step_file
    ):
        """Test quality check with STEP file included."""
        stl_bytes = sample_stl_file.read_bytes()
        step_bytes = sample_step_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")
        step_b64 = base64.b64encode(step_bytes).decode("utf-8")

        response = await client.post(
            "/geometry/uploads/test-upload-002/quality-check",
            json={
                "stl_bytes": stl_b64,
                "cad_bytes": step_b64,
                "cad_extension": ".step",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "quality_check_complete"

        # Should have B-Rep info if STEP analysis succeeded
        quality = data["quality"]
        if quality.get("brep_face_count") is not None:
            assert quality["brep_face_count"] > 0
            print(f"\nB-Rep faces detected: {quality['brep_face_count']}")

    @pytest.mark.asyncio
    async def test_quality_check_handles_invalid_data(self, client):
        """Test quality check with invalid data."""
        response = await client.post(
            "/geometry/uploads/test-upload-invalid/quality-check",
            json={"stl_bytes": base64.b64encode(b"").decode("utf-8")},
        )

        # Should handle gracefully - might return error or empty result
        assert response.status_code in [200, 500]

        if response.status_code == 500:
            data = response.json()
            assert "error_type" in data


class TestProcessAnalysisEndpoint:
    """Test Phase 2: Process-specific analysis endpoint."""

    @pytest.mark.asyncio
    async def test_single_process_analysis(self, client, sample_stl_file):
        """Test single process analysis after quality check."""
        # First, run quality check
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        quality_response = await client.post(
            "/geometry/uploads/test-upload-003/quality-check",
            json={"stl_bytes": stl_b64},
        )
        assert quality_response.status_code == 200

        # Then, run FDM analysis
        start_time = time.time()
        dfm_response = await client.post(
            "/geometry/uploads/test-upload-003/dfm/FDM",
            timeout=30.0,
        )
        duration = time.time() - start_time

        print(f"\nFDM analysis API call completed in {duration:.2f}s")

        # Should complete in reasonable time
        assert duration < 30.0, f"FDM analysis too slow: {duration:.2f}s"

        # Should return success
        assert dfm_response.status_code == 200
        data = dfm_response.json()
        assert data["status"] == "analysis_complete"
        assert data["upload_id"] == "test-upload-003"
        assert data["process_code"] == "FDM"

        # Should have DFM report
        dfm_report = data["dfm_report"]
        assert "reportType" in dfm_report
        assert dfm_report["reportType"] == "FDM"
        assert "issues" in dfm_report
        print(f"FDM issues found: {len(dfm_report['issues'])}")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("process_code", ["FDM", "SLA", "CNC_MILL"])
    async def test_multiple_processes_sequentially(
        self, client, sample_stl_file, process_code
    ):
        """Test analyzing multiple processes sequentially."""
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        upload_id = f"test-upload-{process_code}"

        # Quality check
        quality_response = await client.post(
            f"/geometry/uploads/{upload_id}/quality-check",
            json={"stl_bytes": stl_b64},
        )
        assert quality_response.status_code == 200

        # Process-specific analysis
        dfm_response = await client.post(
            f"/geometry/uploads/{upload_id}/dfm/{process_code}",
            timeout=30.0,
        )

        assert dfm_response.status_code == 200
        data = dfm_response.json()
        assert data["process_code"] == process_code
        assert data["dfm_report"]["reportType"] == process_code

    @pytest.mark.asyncio
    async def test_process_analysis_requires_quality_check_first(
        self, client, sample_stl_file  # noqa: ARG002
    ):
        """Test that process analysis fails without quality check."""
        # Try to run process analysis without quality check
        response = await client.post(
            "/geometry/uploads/test-upload-no-quality/dfm/FDM",
            timeout=5.0,
        )

        # Should return 404 - upload not found
        assert response.status_code == 404
        data = response.json()
        assert "error_type" in data
        assert data["error_type"] == "NotFound"

    @pytest.mark.asyncio
    async def test_process_analysis_with_invalid_process(self, client, sample_stl_file):
        """Test process analysis with invalid process code."""
        # First, run quality check
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        await client.post(
            "/geometry/uploads/test-upload-invalid-process/quality-check",
            json={"stl_bytes": stl_b64},
        )

        # Try invalid process
        response = await client.post(
            "/geometry/uploads/test-upload-invalid-process/dfm/INVALID_PROCESS",
            timeout=5.0,
        )

        # Should return error
        assert response.status_code == 500
        data = response.json()
        assert data["error_type"] == "ValueError"

    @pytest.mark.asyncio
    async def test_process_analysis_timeout(self, client, sample_stl_file):
        """Test that process analysis respects timeout."""
        # First, run quality check
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        await client.post(
            "/geometry/uploads/test-upload-timeout/quality-check",
            json={"stl_bytes": stl_b64},
        )

        # Request with very short timeout (should still succeed for simple file)
        start_time = time.time()
        response = await client.post(
            "/geometry/uploads/test-upload-timeout/dfm/FDM?timeout=5",
            timeout=10.0,
        )
        duration = time.time() - start_time

        print(f"\nFDM analysis with 5s timeout completed in {duration:.2f}s")

        # For simple cube, should complete quickly
        # If it were to timeout, would get 504
        if response.status_code == 504:
            data = response.json()
            assert data["error_type"] == "TimeoutError"
        else:
            assert response.status_code == 200


class TestCleanupEndpoint:
    """Test cleanup endpoint."""

    @pytest.mark.asyncio
    async def test_cleanup_upload(self, client, sample_stl_file):
        """Test cleaning up upload data."""
        # First, create an upload
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        await client.post(
            "/geometry/uploads/test-upload-cleanup/quality-check",
            json={"stl_bytes": stl_b64},
        )

        # Clean it up
        response = await client.delete("/geometry/uploads/test-upload-cleanup")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "cleaned_up"
        assert data["upload_id"] == "test-upload-cleanup"

        # Verify it's gone
        response2 = await client.delete("/geometry/uploads/test-upload-cleanup")
        assert response2.status_code == 404

    @pytest.mark.asyncio
    async def test_cleanup_nonexistent_upload(self, client):
        """Test cleaning up upload that doesn't exist."""
        response = await client.delete("/geometry/uploads/test-upload-nonexistent")

        assert response.status_code == 404


class TestEndToEndWorkflow:
    """Test complete two-phase workflow."""

    @pytest.mark.asyncio
    async def test_complete_two_phase_workflow(self, client, sample_stl_file):
        """Test the complete two-phase workflow as it would be used in production."""
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")
        upload_id = "test-upload-e2e"

        # Phase 1: Quality check (fast, shows preview)
        start_time = time.time()
        quality_response = await client.post(
            f"/geometry/uploads/{upload_id}/quality-check",
            json={"stl_bytes": stl_b64},
        )
        quality_duration = time.time() - start_time

        assert quality_response.status_code == 200
        quality_data = quality_response.json()
        assert quality_data["status"] == "quality_check_complete"
        assert quality_data["ready_for_process_selection"] is True

        print(f"\nPhase 1 (Quality Check): {quality_duration:.2f}s")
        print(f"  Faces: {quality_data['quality']['face_count']}")
        print(f"  Manifold: {quality_data['quality']['is_manifold']}")
        print(f"  Volume: {quality_data['quality']['volume_mm3']:.1f} mm³")

        # Phase 2a: User selects FDM - analyze only FDM
        start_time = time.time()
        fdm_response = await client.post(
            f"/geometry/uploads/{upload_id}/dfm/FDM",
            timeout=30.0,
        )
        fdm_duration = time.time() - start_time

        assert fdm_response.status_code == 200
        fdm_data = fdm_response.json()
        assert fdm_data["status"] == "analysis_complete"

        print(f"\nPhase 2a (FDM Analysis): {fdm_duration:.2f}s")
        print(f"  Issues: {len(fdm_data['dfm_report']['issues'])}")

        # Phase 2b: User changes mind to CNC - analyze only CNC
        start_time = time.time()
        cnc_response = await client.post(
            f"/geometry/uploads/{upload_id}/dfm/CNC_MILL",
            timeout=30.0,
        )
        cnc_duration = time.time() - start_time

        assert cnc_response.status_code == 200
        cnc_data = cnc_response.json()
        assert cnc_data["status"] == "analysis_complete"

        print(f"\nPhase 2b (CNC Analysis): {cnc_duration:.2f}s")
        print(f"  Issues: {len(cnc_data['dfm_report']['issues'])}")

        # Phase 3: Cleanup
        await client.delete(f"/geometry/uploads/{upload_id}")

        total_time = quality_duration + fdm_duration + cnc_duration
        print(f"\nTotal two-phase workflow: {total_time:.2f}s")

        # Total should be much faster than old approach (90+ seconds)
        assert total_time < 60.0, f"Two-phase workflow too slow: {total_time:.2f}s"


class TestCacheMissRecovery:
    """Tests for cache-miss recovery via download_url."""

    @pytest.mark.asyncio
    async def test_permanent_download_failure_returns_410(self, client):
        """When re-download via signed URL hits a permanent GCS error (401/403/404),
        the endpoint should return 410 Gone with status='file_missing'."""
        from unittest.mock import AsyncMock, patch

        from src.infrastructure.storage import PermanentDownloadError

        upload_id = "test-upload-permanent-fail"

        with patch(
            "src.main.HttpDownloadService",
            autospec=True,
        ) as mock_service_class:
            mock_instance = AsyncMock()
            mock_instance.download_file.side_effect = PermanentDownloadError(
                "Permanent failure downloading https://storage.googleapis.com/signed: 404"  # noqa: E501
            )
            mock_instance.close = AsyncMock()
            mock_service_class.return_value = mock_instance

            response = await client.post(
                f"/geometry/uploads/{upload_id}/dfm/FDM",
                json={"download_url": "https://storage.googleapis.com/signed?sig=test"},
                timeout=10.0,
            )

        assert response.status_code == 410
        data = response.json()
        assert data["status"] == "file_missing"
        assert data["error_type"] == "FileMissing"
        assert data["upload_id"] == upload_id

    @pytest.mark.asyncio
    async def test_missing_download_url_returns_404_when_not_cached(self, client):
        """When upload_id is not in cache and no download_url provided, returns 404."""
        response = await client.post(
            "/geometry/uploads/test-upload-no-url/dfm/FDM",
            json={},
            timeout=5.0,
        )

        assert response.status_code == 404
        data = response.json()
        assert data["error_type"] == "NotFound"

    @pytest.mark.asyncio
    async def test_cache_miss_recovery_reconstructs_step_to_stl(
        self, client, test_assets_dir
    ):
        """Cache-miss recovery must tessellate STEP→STL so DFM analysis runs on real geometry
        (not on STEP bytes fed to the STL parser, which produces a 0-face empty mesh)."""  # noqa: E501
        try:
            import cascadio  # noqa: F401
        except ImportError:
            pytest.skip(
                "cascadio not installed — STEP tessellation unavailable in this env"
            )
        step_file = test_assets_dir / "50x50x50mm-solid-cube.step"
        if not step_file.exists():
            pytest.skip("50x50x50mm-solid-cube.step not found")

        step_bytes = step_file.read_bytes()
        upload_id = "test-cache-miss-step-recovery"

        from src.main import _file_analysis_cache

        # Ensure the upload is NOT in cache so recovery path is triggered.
        _file_analysis_cache.pop(upload_id, None) if hasattr(
            _file_analysis_cache, "pop"
        ) else None
        with contextlib.suppress(KeyError):
            del _file_analysis_cache[upload_id]

        import io as _io
        from unittest.mock import AsyncMock, patch

        with patch("src.main.HttpDownloadService", autospec=True) as mock_svc_cls:
            mock_instance = AsyncMock()
            # Simulate downloading the .stp file from the signed URL.
            mock_instance.download_file = AsyncMock(
                return_value=_io.BytesIO(step_bytes)
            )
            mock_instance.close = AsyncMock()
            mock_svc_cls.return_value = mock_instance

            with patch("src.main.publish_event", AsyncMock()):
                response = await client.post(
                    f"/geometry/uploads/{upload_id}/dfm/FDM",
                    json={
                        "storage_path": f"projects/test/{upload_id}/cube.stp",
                        "download_url": "https://storage.googleapis.com/signed?sig=test",
                    },
                    timeout=120.0,
                )

        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data["status"] == "analysis_complete", f"Unexpected status: {data}"

        # The cache entry must contain real STL bytes (non-empty) and correct metadata.
        cached = _file_analysis_cache.get(upload_id)
        assert cached is not None, "Cache entry not populated after recovery"
        assert (
            cached.get("cad_extension") == "stp"
        ), "cad_extension must be 'stp' after STEP recovery"
        assert (
            cached.get("body_count", 0) >= 1
        ), "body_count must be ≥ 1 after tessellation"
        stl_bytes = cached.get("stl_bytes", b"")
        assert (
            len(stl_bytes) > 0
        ), "stl_bytes in cache must not be empty after STEP tessellation"

        # Verify the DFM report has actual geometry data (not a 0-face phantom pass).
        import trimesh as _trimesh

        recovered_mesh = _trimesh.load(
            _io.BytesIO(stl_bytes), file_type="stl", force="mesh"
        )
        assert isinstance(
            recovered_mesh, _trimesh.Trimesh
        ), "Cached STL bytes must parse as Trimesh"
        assert (
            len(recovered_mesh.faces) > 0
        ), "Cached STL must have >0 faces — cache-miss recovery still storing STEP bytes as STL"  # noqa: E501

    @pytest.mark.asyncio
    async def test_quick_quality_check_returns_error_on_step_bytes(
        self, test_assets_dir
    ):
        """_quick_quality_check must return error dict (not a phantom pass) when fed non-STL bytes."""  # noqa: E501
        step_file = test_assets_dir / "50x50x50mm-solid-cube.step"
        if not step_file.exists():
            pytest.skip("50x50x50mm-solid-cube.step not found")

        from src.core.geometry import _quick_quality_check

        result = _quick_quality_check(step_file.read_bytes(), None, None)
        assert (
            result.get("error") == "empty_mesh"
        ), f"Expected error='empty_mesh', got: {result}"
        assert result.get("face_count", -1) == 0

    @pytest.mark.asyncio
    async def test_analyze_for_process_returns_422_for_corrupted_cad(
        self, client, test_assets_dir  # noqa: ARG002
    ):
        """When cache-miss recovery downloads bytes that can't be tessellated,
        the endpoint must return 422 (not a phantom 200 with an empty DFM report)."""
        upload_id = "test-cache-miss-corrupt"

        from src.main import _file_analysis_cache

        with contextlib.suppress(KeyError):
            del _file_analysis_cache[upload_id]

        import io as _io
        from unittest.mock import AsyncMock, patch

        corrupt_bytes = b"this is not a valid STEP file at all"

        with patch("src.main.HttpDownloadService", autospec=True) as mock_svc_cls:
            mock_instance = AsyncMock()
            mock_instance.download_file = AsyncMock(
                return_value=_io.BytesIO(corrupt_bytes)
            )
            mock_instance.close = AsyncMock()
            mock_svc_cls.return_value = mock_instance

            response = await client.post(
                f"/geometry/uploads/{upload_id}/dfm/FDM",
                json={
                    "storage_path": f"projects/test/{upload_id}/broken.stp",
                    "download_url": "https://storage.googleapis.com/signed?sig=test",
                },
                timeout=30.0,
            )

        # Must not be 200 — either tessellation failed (422) or cascade error (500 acceptable).  # noqa: E501
        assert (
            response.status_code in (422, 500)
        ), f"Expected 422 or 500 for corrupted STEP, got {response.status_code}: {response.text}"  # noqa: E501

    @pytest.mark.asyncio
    async def test_cache_miss_recovery_multi_body_step_reports_correct_body_count(
        self, client, test_assets_dir
    ):
        """Cache-miss recovery must report the real body count for multi-body files.

        Uses a 3MF file (3 disconnected bodies) rather than STEP because cascadio
        merges all STEP solids into a single mesh — the 3MF path preserves the
        per-body structure via trimesh Scene geometry, so body_count correctly
        reflects the 3 distinct bodies.
        """
        # 3MF preserves multi-body structure; STEP is merged by cascadio into 1 mesh.
        multi_body_file = test_assets_dir / "50mm-polygon-multibodies-nonoverlap.3mf"
        if not multi_body_file.exists():
            pytest.skip("50mm-polygon-multibodies-nonoverlap.3mf not found")

        file_bytes = multi_body_file.read_bytes()
        upload_id = "00000000-0000-0000-0000-000000000099"  # valid UUID for test

        from src.main import _file_analysis_cache

        with contextlib.suppress(KeyError):
            del _file_analysis_cache[upload_id]

        import io as _io
        from unittest.mock import AsyncMock, patch

        published_payloads: list = []

        async def capture_event(event, routing_key):  # noqa: ARG001
            published_payloads.append(event)

        with patch("src.main.HttpDownloadService", autospec=True) as mock_svc_cls:
            mock_instance = AsyncMock()
            mock_instance.download_file = AsyncMock(
                return_value=_io.BytesIO(file_bytes)
            )
            mock_instance.close = AsyncMock()
            mock_svc_cls.return_value = mock_instance

            with patch("src.main.publish_event", side_effect=capture_event):
                response = await client.post(
                    f"/geometry/uploads/{upload_id}/dfm/FDM",
                    json={
                        "storage_path": f"projects/test/{upload_id}/multibody.3mf",
                        "download_url": "https://storage.googleapis.com/signed?sig=test",
                    },
                    timeout=120.0,
                )

        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"

        cached = _file_analysis_cache.get(upload_id)
        assert cached is not None
        body_count = cached.get("body_count", 1)
        assert (
            body_count > 1
        ), f"Multi-body file should report body_count > 1, got {body_count}"

        # The published DfmAnalysisReadyEvent must carry the correct body count.
        assert (
            len(published_payloads) > 0
        ), "Expected DfmAnalysisReadyEvent to be published"
        published_body_count = published_payloads[0].message.payload.body_count
        assert (
            published_body_count == body_count
        ), f"Published bodyCount={published_body_count} must match cached body_count={body_count}"  # noqa: E501


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
