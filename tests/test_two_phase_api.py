"""Tests for two-phase DFM analysis API endpoints.

Tests the new REST API endpoints for lazy evaluation:
- POST /uploads/{upload_id}/quality-check - Phase 1: Quality checks
- POST /uploads/{upload_id}/dfm/{process_code} - Phase 2: Process-specific analysis
- DELETE /uploads/{upload_id} - Cleanup
"""

import base64
import pytest
import time
from pathlib import Path
from httpx import AsyncClient, ASGITransport

from src.main import app


@pytest.fixture
def test_assets_dir():
    """Get test assets directory."""
    return Path(__file__).parent / "assets"


@pytest.fixture
def sample_stl_file(test_assets_dir):
    """Get path to a sample STL file."""
    stl_file = test_assets_dir / "cube.stl"
    if not stl_file.exists():
        pytest.skip("cube.stl not found")
    return stl_file


@pytest.fixture
def sample_step_file(test_assets_dir):
    """Get path to a sample STEP file."""
    step_file = test_assets_dir / "cover.STEP"
    if not step_file.exists():
        pytest.skip("cover.STEP not found")
    return step_file


@pytest.fixture
async def client():
    """Create async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestQualityCheckEndpoint:
    """Test Phase 1: Quality check endpoint."""

    @pytest.mark.asyncio
    async def test_quality_check_completes_quickly(
        self, client, sample_stl_file
    ):
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
    async def test_single_process_analysis(
        self, client, sample_stl_file
    ):
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
        self, client, sample_stl_file
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
    async def test_process_analysis_with_invalid_process(
        self, client, sample_stl_file
    ):
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


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
