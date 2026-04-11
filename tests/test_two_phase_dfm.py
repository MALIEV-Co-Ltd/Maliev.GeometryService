"""Tests for two-phase DFM analysis architecture.

Tests the new lazy evaluation approach:
- Phase 1: Quick quality checks (<5 seconds)
- Phase 2: Process-specific DFM analysis (<15 seconds per process)
"""

import pytest
import time
from pathlib import Path

from src.core.geometry import (
    _quick_quality_check,
    _analyze_single_process,
    _analyze_single_body,
)


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
    step_file = test_assets_dir / "MEC031233_01.stp"
    if not step_file.exists():
        # Try alternative file names
        step_file = test_assets_dir / "cover.STEP"
    if not step_file.exists():
        pytest.skip("No STEP file found")
    return step_file


@pytest.fixture
def large_step_file(test_assets_dir):
    """Get path to a large STEP file for performance testing."""
    step_file = test_assets_dir / "MEC031233_01.stp"
    if not step_file.exists():
        pytest.skip("MEC031233_01.stp not found")
    return step_file


class TestQuickQualityCheck:
    """Test Phase 1: Quick quality checks."""

    def test_quality_check_completes_quickly(self, sample_stl_file):
        """Test that quality check completes in <5 seconds."""
        stl_bytes = sample_stl_file.read_bytes()

        start_time = time.time()
        result = _quick_quality_check(stl_bytes)
        duration = time.time() - start_time

        print(f"\nQuality check completed in {duration:.2f}s")

        # Should complete quickly
        assert duration < 5.0, f"Quality check too slow: {duration:.2f}s"

        # Should have expected fields
        assert "is_manifold" in result
        assert "is_empty" in result
        assert "face_count" in result
        assert "volume_mm3" in result
        assert "bounding_box" in result
        assert "can_preview" in result

        # Should not have errors
        assert "error" not in result

    def test_quality_check_with_step_file(self, sample_step_file):
        """Test quality check with STEP file (includes B-Rep face count)."""
        stl_bytes = (Path(sample_step_file).parent / "cube.stl").read_bytes()

        # Try to find matching STL
        stl_file = Path(sample_step_file).parent / f"{sample_step_file.stem}.stl"
        if not stl_file.exists():
            stl_file = Path(__file__).parent.parent / "tests" / "assets" / "cube.stl"

        if not stl_file.exists():
            pytest.skip("No matching STL file found")

        step_bytes = sample_step_file.read_bytes()
        stl_bytes = stl_file.read_bytes()

        start_time = time.time()
        result = _quick_quality_check(stl_bytes, step_bytes, ".stp")
        duration = time.time() - start_time

        print(f"\nQuality check with STEP completed in {duration:.2f}s")

        # Should complete quickly even with STEP
        assert duration < 5.0, f"Quality check too slow: {duration:.2f}s"

        # Should have B-Rep face count if STEP analysis succeeded
        if result.get("brep_face_count") is not None:
            assert result["brep_face_count"] > 0
            print(f"B-Rep faces: {result['brep_face_count']}")

    def test_quality_check_returns_correct_metrics(self, sample_stl_file):
        """Test that quality check returns accurate metrics."""
        stl_bytes = sample_stl_file.read_bytes()

        result = _quick_quality_check(stl_bytes)

        # Basic sanity checks
        assert isinstance(result["is_manifold"], bool)
        assert isinstance(result["is_empty"], bool)
        assert isinstance(result["face_count"], int)
        assert result["face_count"] > 0
        assert isinstance(result["vertex_count"], int)
        assert result["vertex_count"] > 0

        # Volume should be positive for watertight mesh
        if result["is_manifold"]:
            assert result["volume_mm3"] > 0

        # Bounding box should have positive dimensions
        bbox = result["bounding_box"]
        assert bbox["x"] > 0
        assert bbox["y"] > 0
        assert bbox["z"] > 0

        # Complexity should be one of the expected values
        assert result["complexity"] in ("simple", "medium", "complex")

        # Can preview should be True for valid mesh
        assert result["can_preview"] is True

    def test_quality_check_handles_invalid_data(self):
        """Test that quality check handles invalid data gracefully."""
        result = _quick_quality_check(b"")

        # Should return a result indicating empty/invalid mesh
        assert "is_manifold" in result
        assert "is_empty" in result

        # Empty bytes should result in is_empty=True
        # or have an error field (depends on trimesh behavior)
        assert result["is_empty"] is True or "error" in result


class TestProcessSpecificAnalysis:
    """Test Phase 2: Process-specific DFM analysis."""

    @pytest.mark.parametrize("process_code", ["FDM", "SLA", "CNC_MILL"])
    def test_single_process_analysis_completes_quickly(
        self, sample_stl_file, process_code
    ):
        """Test that single process analysis completes in <15 seconds."""
        stl_bytes = sample_stl_file.read_bytes()

        start_time = time.time()
        result = _analyze_single_process(stl_bytes, process_code)
        duration = time.time() - start_time

        print(f"\n{process_code} analysis completed in {duration:.2f}s")

        # Should complete in reasonable time
        assert duration < 15.0, f"{process_code} analysis too slow: {duration:.2f}s"

        # Should not be an error
        assert "error_type" not in result, f"Got error: {result.get('message')}"

        # Should have report structure
        assert "reportType" in result
        assert result["reportType"] == process_code
        assert "issues" in result

    def test_single_process_returns_only_requested_process(self, sample_stl_file):
        """Test that single process analysis returns only one process."""
        stl_bytes = sample_stl_file.read_bytes()

        result_fdm = _analyze_single_process(stl_bytes, "FDM")
        result_sla = _analyze_single_process(stl_bytes, "SLA")

        # Each should have only the requested process
        assert result_fdm["reportType"] == "FDM"
        assert result_sla["reportType"] == "SLA"

        # Issues should be different between processes
        fdm_issues = result_fdm["issues"]
        sla_issues = result_sla["issues"]

        print(f"\nFDM issues: {len(fdm_issues)}")
        print(f"SLA issues: {len(sla_issues)}")

    def test_single_process_with_shared_precomputed(self, sample_stl_file):
        """Test that shared pre-computed data avoids redundant computation."""
        from src.core.mesh_analyzers import detect_hollow_regions, detect_holes_mesh

        stl_bytes = sample_stl_file.read_bytes()

        # Pre-compute shared data
        import trimesh
        import io

        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        hollow_centroids, _ = detect_hollow_regions(mesh)
        all_holes = detect_holes_mesh(mesh, min_diameter_mm=1.0)

        shared_precomputed = {
            "hollow_centroids": hollow_centroids,
            "all_holes": all_holes,
        }

        # Run analysis with shared data
        start_time = time.time()
        result = _analyze_single_process(
            stl_bytes, "FDM", shared_precomputed=shared_precomputed
        )
        duration = time.time() - start_time

        print(f"\nFDM analysis with shared data completed in {duration:.2f}s")

        # Should complete successfully
        assert "error_type" not in result
        assert result["reportType"] == "FDM"

    def test_single_process_handles_invalid_process_code(self, sample_stl_file):
        """Test that invalid process code returns error."""
        stl_bytes = sample_stl_file.read_bytes()

        result = _analyze_single_process(stl_bytes, "INVALID_PROCESS")

        assert result["error_type"] == "ValueError"
        assert "Unknown process code" in result["message"]

    def test_single_process_with_step_file(self, sample_step_file):
        """Test process-specific analysis with STEP file."""
        # Find matching STL
        stl_file = Path(sample_step_file).parent / f"{sample_step_file.stem}.stl"
        if not stl_file.exists():
            stl_file = (
                Path(__file__).parent.parent / "tests" / "assets" / "cube.stl"
            )

        if not stl_file.exists():
            pytest.skip("No matching STL file found")

        stl_bytes = stl_file.read_bytes()
        step_bytes = sample_step_file.read_bytes()

        start_time = time.time()
        result = _analyze_single_process(stl_bytes, "CNC_MILL", step_bytes, ".stp")
        duration = time.time() - start_time

        print(f"\nCNC_MILL analysis with STEP completed in {duration:.2f}s")

        # Should complete in reasonable time
        assert duration < 30.0, f"CNC_MILL with STEP too slow: {duration:.2f}s"

        # Should have CNC-specific issues
        assert "error_type" not in result
        assert result["reportType"] == "CNC_MILL"
        print(f"CNC_MILL issues: {len(result['issues'])}")


class TestPerformanceComparison:
    """Compare performance of old vs. new architecture."""

    def test_quality_check_vs_full_analysis(self, sample_stl_file):
        """Compare quality check speed vs. full analysis."""
        stl_bytes = sample_stl_file.read_bytes()

        # Measure quality check (new architecture)
        start_time = time.time()
        quality_result = _quick_quality_check(stl_bytes)
        quality_duration = time.time() - start_time

        # Measure full analysis (old architecture)
        start_time = time.time()
        full_result = _analyze_single_body(stl_bytes)
        full_duration = time.time() - start_time

        print(f"\nQuality check: {quality_duration:.2f}s")
        print(f"Full analysis: {full_duration:.2f}s")
        print(f"Speedup: {full_duration / quality_duration:.1f}x")

        # Quality check should be significantly faster
        assert quality_duration < full_duration, "Quality check should be faster"

    def test_single_process_vs_all_processes(self, sample_stl_file):
        """Compare single process vs. all processes."""
        stl_bytes = sample_stl_file.read_bytes()

        # Measure single process analysis
        start_time = time.time()
        single_result = _analyze_single_process(stl_bytes, "FDM")
        single_duration = time.time() - start_time

        # Measure all processes analysis
        start_time = time.time()
        all_result = _analyze_single_body(stl_bytes)
        all_duration = time.time() - start_time

        print(f"\nSingle process (FDM): {single_duration:.2f}s")
        print(f"All processes: {all_duration:.2f}s")
        print(f"Number of processes: {len(all_result)}")

        # Single process should be faster than all processes
        assert single_duration < all_duration, "Single process should be faster"

        # All processes should include FDM
        assert "FDM" in all_result


class TestProductionFilePerformance:
    """Test performance with production files that previously timed out."""

    def test_large_step_file_quality_check(self, large_step_file):
        """Test quality check on large STEP file that previously timed out."""
        # Find matching STL or use cube.stl as fallback
        stl_file = large_step_file.parent / f"{large_step_file.stem}.stl"
        if not stl_file.exists():
            # Use cube.stl as fallback (geometry will be wrong but tests the pipeline)
            stl_file = large_step_file.parent / "cube.stl"
            if not stl_file.exists():
                pytest.skip("No STL file found")

        stl_bytes = stl_file.read_bytes()
        step_bytes = large_step_file.read_bytes()

        print(f"\nTesting {large_step_file.name}")
        print(f"File size: {len(stl_bytes) / 1024:.1f}KB STL, {len(step_bytes) / 1024:.1f}KB STEP")

        # Quality check should be fast
        start_time = time.time()
        quality_result = _quick_quality_check(stl_bytes, step_bytes, ".stp")
        quality_duration = time.time() - start_time

        print(f"Quality check: {quality_duration:.2f}s")
        print(f"Faces: {quality_result.get('face_count')}")
        print(f"Complexity: {quality_result.get('complexity')}")
        print(f"B-Rep faces: {quality_result.get('brep_face_count')}")

        # Should complete quickly
        assert quality_duration < 5.0, f"Quality check too slow: {quality_duration:.2f}s"

    def test_large_step_file_single_process(self, large_step_file):
        """Test single process analysis on large STEP file."""
        # Find matching STL or use cube.stl as fallback
        stl_file = large_step_file.parent / f"{large_step_file.stem}.stl"
        if not stl_file.exists():
            # Use cube.stl as fallback (geometry will be wrong but tests the pipeline)
            stl_file = large_step_file.parent / "cube.stl"
            if not stl_file.exists():
                pytest.skip("No STL file found")

        stl_bytes = stl_file.read_bytes()
        step_bytes = large_step_file.read_bytes()

        # Single process should be reasonable
        start_time = time.time()
        result = _analyze_single_process(stl_bytes, "FDM", step_bytes, ".stp")
        duration = time.time() - start_time

        print(f"\nFDM analysis: {duration:.2f}s")
        print(f"Issues: {len(result.get('issues', []))}")

        # Should complete in reasonable time (not 90+ seconds)
        # Note: May take longer if using mismatched STL/STEP files
        assert duration < 30.0, f"FDM analysis too slow: {duration:.2f}s"
        assert "error_type" not in result


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
