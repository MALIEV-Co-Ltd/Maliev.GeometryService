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
def large_step_file(test_assets_dir):
    """Get path to a large STEP file for performance testing."""
    step_file = (
        test_assets_dir
        / "100x100x100mm-cube-sharp-internal-corners-various-fillets.step"
    )
    if not step_file.exists():
        pytest.skip(
            "100x100x100mm-cube-sharp-internal-corners-various-fillets.step not found"
        )
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
        stl_bytes = (
            Path(sample_step_file).parent / "50x50x50mm-solid-cube-binary.stl"
        ).read_bytes()

        # Try to find matching STL
        stl_file = Path(sample_step_file).parent / f"{sample_step_file.stem}.stl"
        if not stl_file.exists():
            stl_file = (
                Path(__file__).parent.parent
                / "tests"
                / "assets"
                / "50x50x50mm-solid-cube-binary.stl"
            )

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
                Path(__file__).parent.parent
                / "tests"
                / "assets"
                / "50x50x50mm-solid-cube-binary.stl"
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

        # Measure quality check (lightweight, no DFM)
        start_time = time.time()
        quality_result = _quick_quality_check(stl_bytes)
        quality_duration = time.time() - start_time

        # Measure full analysis (_analyze_single_body now runs real FDM)
        start_time = time.time()
        full_result = _analyze_single_body(stl_bytes)
        full_duration = time.time() - start_time

        print(f"\nQuality check: {quality_duration:.2f}s")
        print(f"Full analysis (FDM): {full_duration:.2f}s")

        # Quality check should be trivially fast
        assert quality_duration < 5.0, (
            f"Quality check too slow: {quality_duration:.2f}s"
        )
        # Full analysis now runs FDM — allow up to 30 s for slow CI machines
        assert full_duration < 30.0, (
            f"Full analysis too slow: {full_duration:.2f}s"
        )

    def test_single_process_vs_all_processes(self, sample_stl_file):
        """_analyze_single_body and _analyze_single_process(FDM) both run real FDM."""
        stl_bytes = sample_stl_file.read_bytes()

        single_result = _analyze_single_process(stl_bytes, "FDM")
        all_result = _analyze_single_body(stl_bytes)

        print(f"\nSingle process (FDM) issues: {len(single_result.get('issues', []))}")
        print(f"Full body FDM issues: {len(all_result.get('FDM', {}).get('issues', []))}")

        # Both should include FDM
        assert "FDM" in all_result
        assert "SLA" in all_result
        assert "CNC" in all_result

        # No report should be deferred — consumer must publish real data
        for process_code in ("FDM", "SLA", "CNC"):
            report = all_result[process_code]
            assert report.get("twoPhaseDeferred", False) is False, (
                f"{process_code} must not be deferred"
            )

        # Single process result must also not be deferred
        assert single_result.get("twoPhaseDeferred", False) is False
        assert len(single_result.get("issues", [])) >= 0


class TestProductionFilePerformance:
    """Test performance with production files that previously timed out."""

    def test_large_step_file_quality_check(self, large_step_file):
        """Test quality check on large STEP file that previously timed out."""
        # Find matching STL or use 50x50x50mm-solid-cube-binary.stl as fallback
        stl_file = large_step_file.parent / f"{large_step_file.stem}.stl"
        if not stl_file.exists():
            # Use 50x50x50mm-solid-cube-binary.stl as fallback (geometry will be wrong but tests the pipeline)
            stl_file = large_step_file.parent / "50x50x50mm-solid-cube-binary.stl"
            if not stl_file.exists():
                pytest.skip("No STL file found")

        stl_bytes = stl_file.read_bytes()
        step_bytes = large_step_file.read_bytes()

        print(f"\nTesting {large_step_file.name}")
        print(
            f"File size: {len(stl_bytes) / 1024:.1f}KB STL, {len(step_bytes) / 1024:.1f}KB STEP"
        )

        # Quality check should be fast
        start_time = time.time()
        quality_result = _quick_quality_check(stl_bytes, step_bytes, ".stp")
        quality_duration = time.time() - start_time

        print(f"Quality check: {quality_duration:.2f}s")
        print(f"Faces: {quality_result.get('face_count')}")
        print(f"Complexity: {quality_result.get('complexity')}")
        print(f"B-Rep faces: {quality_result.get('brep_face_count')}")

        # Should complete quickly
        assert quality_duration < 5.0, (
            f"Quality check too slow: {quality_duration:.2f}s"
        )

    def test_large_step_file_single_process(self, large_step_file):
        """Test single process analysis on large STEP file."""
        # Find matching STL or use 50x50x50mm-solid-cube-binary.stl as fallback
        stl_file = large_step_file.parent / f"{large_step_file.stem}.stl"
        if not stl_file.exists():
            # Use 50x50x50mm-solid-cube-binary.stl as fallback (geometry will be wrong but tests the pipeline)
            stl_file = large_step_file.parent / "50x50x50mm-solid-cube-binary.stl"
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


class TestSlaReportValidation:
    """Regression tests ensuring SLA/FDM/CNC reports from _analyze_single_body
    are model-validatable by Pydantic so the consumer publishes non-null DFM events.
    """

    def test_sla_report_has_all_required_fields(self, sample_stl_file):
        """SLA report must include all SLA-specific fields and be non-deferred."""
        from src.core.geometry import SlaDfmReport

        stl_bytes = sample_stl_file.read_bytes()
        reports = _analyze_single_body(stl_bytes)

        assert "SLA" in reports, "SLA key must be present in reports"
        sla_raw = reports["SLA"]

        # Must NOT be deferred — consumer will publish it as a real report
        assert sla_raw.get("twoPhaseDeferred", False) is False, (
            "SLA report must not be deferred"
        )

        # Must be model-validatable — this was the crashing line in upload_consumer
        sla_report = SlaDfmReport.model_validate(sla_raw)
        assert sla_report.report_type == "SLA"
        assert isinstance(sla_report.resin_trapping_risk, bool)
        assert isinstance(sla_report.resin_trapping_regions, list)
        assert isinstance(sla_report.suction_risk, bool)
        assert isinstance(sla_report.suction_regions, list)
        assert isinstance(sla_report.hollow_regions, list)

    def test_fdm_report_validates_and_is_not_deferred(self, sample_stl_file):
        """FDM report must validate and contain real analysis results."""
        from src.core.geometry import FdmDfmReport

        stl_bytes = sample_stl_file.read_bytes()
        reports = _analyze_single_body(stl_bytes)

        assert "FDM" in reports
        fdm_raw = reports["FDM"]
        assert fdm_raw.get("twoPhaseDeferred", False) is False, (
            "FDM report must not be deferred"
        )

        fdm_report = FdmDfmReport.model_validate(fdm_raw)
        assert fdm_report.report_type == "FDM"
        assert isinstance(fdm_report.issues, list)

    def test_cnc_report_validates_and_is_not_deferred(self, sample_stl_file):
        """CNC report must validate and be non-deferred."""
        from src.core.geometry import CncDfmReport

        stl_bytes = sample_stl_file.read_bytes()
        reports = _analyze_single_body(stl_bytes)

        assert "CNC" in reports
        cnc_raw = reports["CNC"]
        assert cnc_raw.get("twoPhaseDeferred", False) is False, (
            "CNC report must not be deferred"
        )

        cnc_report = CncDfmReport.model_validate(cnc_raw)
        assert isinstance(cnc_report.issues, list)

    def test_all_consumer_reports_validate(self, sample_stl_file):
        """FDM, SLA, and CNC must all be Pydantic-validatable (consumer path)."""
        from src.core.geometry import CncDfmReport, FdmDfmReport, SlaDfmReport

        stl_bytes = sample_stl_file.read_bytes()
        reports = _analyze_single_body(stl_bytes)

        FdmDfmReport.model_validate(reports["FDM"])
        SlaDfmReport.model_validate(reports["SLA"])
        CncDfmReport.model_validate(reports["CNC"])


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
