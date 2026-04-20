"""Tests for two-phase DFM analysis API functions.

Tests the new API functions for lazy evaluation without requiring full FastAPI setup.
"""

import base64
import pytest
import time
from pathlib import Path

from src.main import (
    quality_check,
    analyze_for_process,
    cleanup_upload,
    _file_analysis_cache,
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

    @pytest.mark.anyio
    async def test_single_process_analysis(self, sample_stl_file):
        """Test single process analysis after quality check."""
        # First, run quality check
        stl_bytes = sample_stl_file.read_bytes()
        stl_b64 = base64.b64encode(stl_bytes).decode("utf-8")

        await quality_check("test-upload-002", {"stl_bytes": stl_b64})

        # Then, run FDM analysis
        start_time = time.time()
        response = await analyze_for_process("test-upload-002", "FDM", timeout=30)
        duration = time.time() - start_time

        print(f"\nFDM analysis API call completed in {duration:.2f}s")

        # Should complete in reasonable time
        assert duration < 30.0, f"FDM analysis too slow: {duration:.2f}s"

        # Should return success
        assert response.status_code == 200
        data = response.body.decode()
        assert "analysis_complete" in data
        assert "FDM" in data

    @pytest.mark.anyio
    async def test_process_analysis_requires_quality_check_first(self):
        """Test that process analysis fails without quality check."""
        # Try to run process analysis without quality check
        response = await analyze_for_process("test-upload-no-quality", "FDM", timeout=5)

        # Should return 404 - upload not found
        assert response.status_code == 404
        data = response.body.decode()
        assert "NotFound" in data


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
        fdm_response = await analyze_for_process(upload_id, "FDM", timeout=30)
        fdm_duration = time.time() - start_time

        assert fdm_response.status_code == 200
        fdm_data = fdm_response.body.decode()
        assert "analysis_complete" in fdm_data

        print(f"Phase 2a (FDM Analysis): {fdm_duration:.2f}s")

        # Phase 2b: User changes mind to CNC - analyze only CNC
        start_time = time.time()
        cnc_response = await analyze_for_process(upload_id, "CNC_MILL", timeout=30)
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
