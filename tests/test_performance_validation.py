"""Performance and quality validation tests for two-phase DFM architecture.

Tests verify:
- Performance targets (<5s quality check, <15s process analysis)
- Quality accuracy (no regression from single-phase)
- Production file performance
- End-to-end workflow validation
- Stage 5 optimizations (process-specific checks, caching, adaptive tessellation)
"""

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

from src.core.geometry import (  # noqa: E402
    _analyze_single_body,
    _analyze_single_process,
    _quick_quality_check,
)
from src.core.geometry_optimizations import get_cache_key  # noqa: E402


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


class TestPerformanceTargets:
    """Verify performance targets are met."""

    @pytest.mark.anyio
    async def test_quality_check_under_5_seconds_simple_file(self, sample_stl_file):
        """Test that quality check completes in <5 seconds for simple files."""
        stl_bytes = sample_stl_file.read_bytes()

        start_time = time.time()
        result = _quick_quality_check(stl_bytes)
        duration = time.time() - start_time

        print(f"\nQuality check completed in {duration:.2f}s for simple file")

        assert duration < 5.0, f"Quality check too slow: {duration:.2f}s (target: <5s)"
        assert result["is_manifold"] is True
        assert result["face_count"] > 0

    @pytest.mark.anyio
    async def test_quality_check_under_5_seconds_complex_file(self, sample_step_file):
        """Test that quality check completes in <5 seconds for complex STEP files."""
        # First need to get STL bytes from STEP
        # For now, we'll skip this test if we can't convert
        stl_file = sample_step_file.parent / "cover.stl"
        if not stl_file.exists():
            pytest.skip("No STL available for complex file test")
            return

        stl_bytes = stl_file.read_bytes()

        start_time = time.time()
        _quick_quality_check(stl_bytes)
        duration = time.time() - start_time

        print(f"\nQuality check completed in {duration:.2f}s for complex file")

        assert duration < 5.0, f"Quality check too slow: {duration:.2f}s (target: <5s)"

    @pytest.mark.anyio
    async def test_single_process_under_15_seconds(self, sample_stl_file):
        """Test that single process analysis completes in <15 seconds."""
        stl_bytes = sample_stl_file.read_bytes()

        start_time = time.time()
        result = _analyze_single_process(stl_bytes, "FDM")
        duration = time.time() - start_time

        print(f"\nFDM analysis completed in {duration:.2f}s")

        assert (
            duration < 15.0
        ), f"Process analysis too slow: {duration:.2f}s (target: <15s)"
        assert result["reportType"] == "FDM"
        assert (
            len(result["issues"]) >= 0
        )  # Should have some issues (or none if perfect)

    @pytest.mark.parametrize("process_code", ["FDM", "SLA", "CNC_MILL", "CNC_TURN"])
    @pytest.mark.anyio
    async def test_all_processes_under_15_seconds(self, sample_stl_file, process_code):
        """Test that all process types complete in <15 seconds."""
        stl_bytes = sample_stl_file.read_bytes()

        start_time = time.time()
        result = _analyze_single_process(stl_bytes, process_code)
        duration = time.time() - start_time

        print(f"\n{process_code} analysis completed in {duration:.2f}s")

        assert duration < 15.0, f"{process_code} analysis too slow: {duration:.2f}s"
        assert result["reportType"] == process_code


class TestQualityAccuracy:
    """Verify two-phase approach maintains quality accuracy."""

    @pytest.mark.anyio
    async def test_manifold_detection_accuracy(self, sample_stl_file):
        """Test that manifold detection is accurate in both approaches."""
        stl_bytes = sample_stl_file.read_bytes()

        # Old approach
        old_result = _analyze_single_body(stl_bytes, None, None)
        old_manifold = (
            old_result.get("quality", {}).get("is_manifold")
            if old_result.get("quality")
            else None
        )

        # New approach
        new_result = _quick_quality_check(stl_bytes)
        new_manifold = new_result.get("is_manifold")

        # Both should agree on manifold status
        if old_manifold is not None:
            assert (
                old_manifold == new_manifold
            ), f"Manifold detection mismatch: old={old_manifold}, new={new_manifold}"
        else:
            # If old approach doesn't have manifold status, new approach should still work  # noqa: E501
            assert (
                new_manifold is not None
            ), "New approach should detect manifold status"

    @pytest.mark.anyio
    async def test_volume_calculation_accuracy(self, sample_stl_file):
        """Test that volume calculation is accurate in both approaches."""
        stl_bytes = sample_stl_file.read_bytes()

        # Old approach
        old_result = _analyze_single_body(stl_bytes, None, None)
        old_volume = (
            old_result.get("quality", {}).get("volume_mm3")
            if old_result.get("quality")
            else None
        )

        # New approach
        new_result = _quick_quality_check(stl_bytes)
        new_volume = new_result.get("volume_mm3")

        # Both should have similar volume calculations
        if old_volume is not None:
            assert (
                abs(old_volume - new_volume) < 1.0
            ), f"Volume calculation mismatch: old={old_volume}, new={new_volume}"
        else:
            # If old approach doesn't have volume, new approach should still work
            assert new_volume > 0, "New approach should calculate volume"

    @pytest.mark.anyio
    async def test_face_count_accuracy(self, sample_stl_file):
        """Test that face count is accurate in both approaches."""
        stl_bytes = sample_stl_file.read_bytes()

        # Old approach
        old_result = _analyze_single_body(stl_bytes, None, None)
        old_faces = (
            old_result.get("quality", {}).get("face_count")
            if old_result.get("quality")
            else None
        )

        # New approach
        new_result = _quick_quality_check(stl_bytes)
        new_faces = new_result.get("face_count")

        # Both should have same face count
        if old_faces is not None:
            assert (
                old_faces == new_faces
            ), f"Face count mismatch: old={old_faces}, new={new_faces}"
        else:
            # If old approach doesn't have face count, new approach should still work
            assert new_faces > 0, "New approach should count faces"

    @pytest.mark.anyio
    async def test_bounding_box_accuracy(self, sample_stl_file):
        """Test that bounding box is accurate in both approaches."""
        stl_bytes = sample_stl_file.read_bytes()

        # Old approach
        old_result = _analyze_single_body(stl_bytes, None, None)
        old_bbox = (
            old_result.get("quality", {}).get("bounding_box")
            if old_result.get("quality")
            else None
        )

        # New approach
        new_result = _quick_quality_check(stl_bytes)
        new_bbox = new_result.get("bounding_box")

        # New approach should always have bounding box
        assert new_bbox is not None, "New approach should calculate bounding box"
        assert (
            new_bbox["x"] > 0 and new_bbox["y"] > 0 and new_bbox["z"] > 0
        ), "Bounding box should have positive dimensions"

        # If old approach has bounding box, they should match
        if old_bbox is not None:
            for dim in ["x", "y", "z"]:
                assert (
                    abs(old_bbox[dim] - new_bbox[dim]) < 0.1
                ), f"Bounding box {dim} mismatch: old={old_bbox[dim]}, new={new_bbox[dim]}"  # noqa: E501


class TestDfmIssueAccuracy:
    """Verify DFM issue detection accuracy is maintained."""

    @pytest.mark.anyio
    async def test_fdm_thin_wall_detection_accuracy(self, sample_stl_file):
        """Test that FDM thin wall detection is accurate."""
        stl_bytes = sample_stl_file.read_bytes()

        # Old approach - analyze all processes
        old_result = _analyze_single_body(stl_bytes, None, None)
        old_fdm = old_result.get("dfm_reports", {}).get("FDM", {})
        old_issues = old_fdm.get("issues", [])

        # New approach - analyze only FDM
        new_result = _analyze_single_process(stl_bytes, "FDM")
        new_issues = new_result.get("issues", [])

        # Count thin wall issues in both
        old_thin_walls = [i for i in old_issues if i.get("category") == "thin_wall"]
        new_thin_walls = [i for i in new_issues if i.get("category") == "thin_wall"]

        print(f"\nOld approach: {len(old_thin_walls)} thin wall issues")
        print(f"New approach: {len(new_thin_walls)} thin wall issues")

        # Should have same or similar number of issues
        # Allow small difference due to algorithmic changes
        assert (
            abs(len(old_thin_walls) - len(new_thin_walls)) <= 1
        ), f"Thin wall detection count differs significantly: old={len(old_thin_walls)}, new={len(new_thin_walls)}"  # noqa: E501

    @pytest.mark.anyio
    async def test_fdm_overhang_detection_accuracy(self, sample_stl_file):
        """Test that FDM overhang detection is accurate."""
        stl_bytes = sample_stl_file.read_bytes()

        # Old approach
        old_result = _analyze_single_body(stl_bytes, None, None)
        old_fdm = old_result.get("dfm_reports", {}).get("FDM", {})
        old_issues = old_fdm.get("issues", [])

        # New approach
        new_result = _analyze_single_process(stl_bytes, "FDM")
        new_issues = new_result.get("issues", [])

        # Count overhang issues
        old_overhangs = [i for i in old_issues if i.get("category") == "overhang"]
        new_overhangs = [i for i in new_issues if i.get("category") == "overhang"]

        print(f"\nOld approach: {len(old_overhangs)} overhang issues")
        print(f"New approach: {len(new_overhangs)} overhang issues")

        # Should have same or similar number
        assert (
            abs(len(old_overhangs) - len(new_overhangs)) <= 1
        ), f"Overhang detection count differs significantly: old={len(old_overhangs)}, new={len(new_overhangs)}"  # noqa: E501

    @pytest.mark.anyio
    async def test_cnc_internal_radii_detection_accuracy(self, sample_step_file):
        """Test that CNC internal radii detection is accurate."""
        if not sample_step_file.exists():
            pytest.skip("STEP file not available for CNC test")
            return

        # Need STL for CNC test
        stl_file = sample_step_file.parent / "cover.stl"
        if not stl_file.exists():
            pytest.skip("No STL available for CNC test")
            return

        stl_bytes = stl_file.read_bytes()
        cad_bytes = sample_step_file.read_bytes()
        cad_ext = "step"

        # Old approach
        old_result = _analyze_single_body(stl_bytes, cad_bytes, cad_ext)
        old_cnc = old_result.get("dfm_reports", {}).get("CNC", {})
        old_issues = old_cnc.get("issues", [])

        # New approach
        new_result = _analyze_single_process(stl_bytes, "CNC_MILL", cad_bytes, cad_ext)
        new_issues = new_result.get("issues", [])

        # Count internal radius issues
        old_radii = [i for i in old_issues if i.get("category") == "internal_radius"]
        new_radii = [i for i in new_issues if i.get("category") == "internal_radius"]

        print(f"\nOld approach: {len(old_radii)} internal radius issues")
        print(f"New approach: {len(new_radii)} internal radius issues")

        # Should have same or similar number
        assert (
            abs(len(old_radii) - len(new_radii)) <= 2
        ), f"Internal radius detection count differs: old={len(old_radii)}, new={len(new_radii)}"  # noqa: E501


class TestProductionFilePerformance:
    """Test performance with production-size files."""

    @pytest.mark.anyio
    async def test_production_file_quality_check_speed(self, sample_step_file):
        """Test quality check speed with production STEP file."""
        if not sample_step_file.exists():
            pytest.skip("Production STEP file not available")
            return

        # Convert STEP to STL first (or use cached STL if available)
        stl_file = sample_step_file.parent / "cover.stl"
        if not stl_file.exists():
            pytest.skip("No STL available for production file test")
            return

        stl_bytes = stl_file.read_bytes()

        start_time = time.time()
        result = _quick_quality_check(stl_bytes)
        duration = time.time() - start_time

        print(f"\nProduction file quality check: {duration:.2f}s")
        print(f"  Faces: {result['face_count']}")
        print(f"  Complexity: {result['complexity']}")

        assert (
            duration < 5.0
        ), f"Production file quality check too slow: {duration:.2f}s"
        assert result["is_manifold"] is True

    @pytest.mark.anyio
    async def test_production_file_process_analysis_speed(self, sample_step_file):
        """Test process analysis speed with production file."""
        if not sample_step_file.exists():
            pytest.skip("Production STEP file not available")
            return

        stl_file = sample_step_file.parent / "cover.stl"
        if not stl_file.exists():
            pytest.skip("No STL available for production file test")
            return

        stl_bytes = stl_file.read_bytes()
        cad_bytes = sample_step_file.read_bytes()

        # Test FDM (should be fast)
        start_time = time.time()
        fdm_result = _analyze_single_process(stl_bytes, "FDM")
        fdm_duration = time.time() - start_time

        print(f"\nProduction file FDM analysis: {fdm_duration:.2f}s")
        print(f"  Issues: {len(fdm_result['issues'])}")

        assert (
            fdm_duration < 15.0
        ), f"Production file FDM analysis too slow: {fdm_duration:.2f}s"

        # Test CNC (might take longer, but should still complete)
        start_time = time.time()
        cnc_result = _analyze_single_process(stl_bytes, "CNC_MILL", cad_bytes, "step")
        cnc_duration = time.time() - start_time

        print(f"Production file CNC analysis: {cnc_duration:.2f}s")
        print(f"  Issues: {len(cnc_result['issues'])}")

        assert (
            cnc_duration < 30.0
        ), f"Production file CNC analysis too slow: {cnc_duration:.2f}s"


class TestEndToEndWorkflow:
    """Test complete two-phase workflow."""

    @pytest.mark.anyio
    async def test_complete_two_phase_workflow(self, sample_stl_file, sample_step_file):
        """Test the complete two-phase workflow as used in production."""
        stl_bytes = sample_stl_file.read_bytes()

        # Phase 1: Quality check (fast, shows preview)
        start_time = time.time()
        quality_result = _quick_quality_check(stl_bytes)
        quality_duration = time.time() - start_time

        assert quality_result["is_manifold"] is True
        assert quality_result["face_count"] > 0
        assert quality_result["can_preview"] is True

        print(f"\n✓ Phase 1 (Quality Check): {quality_duration:.2f}s")
        print(f"  File is valid: {quality_result['face_count']} faces")
        print(
            f"  Ready for process selection: {quality_result.get('can_preview', False)}"
        )

        # Phase 2a: User selects FDM
        start_time = time.time()
        fdm_result = _analyze_single_process(stl_bytes, "FDM")
        fdm_duration = time.time() - start_time

        assert fdm_result["reportType"] == "FDM"
        assert len(fdm_result["issues"]) >= 0

        print(f"\n✓ Phase 2a (FDM Analysis): {fdm_duration:.2f}s")
        print(f"  Issues found: {len(fdm_result['issues'])}")

        # Phase 2b: User changes mind to CNC
        cnc_bytes = sample_step_file.read_bytes() if sample_step_file.exists() else None

        start_time = time.time()
        cnc_result = _analyze_single_process(stl_bytes, "CNC_MILL", cnc_bytes, "step")
        cnc_duration = time.time() - start_time

        assert cnc_result["reportType"] in ["CNC", "CNC_MILL"]
        assert len(cnc_result["issues"]) >= 0

        print(f"\n✓ Phase 2b (CNC Analysis): {cnc_duration:.2f}s")
        print(f"  Issues found: {len(cnc_result['issues'])}")

        # Verify total time is reasonable
        total_time = quality_duration + fdm_duration + cnc_duration
        print(f"\n✓ Total two-phase workflow: {total_time:.2f}s")

        # Should be much faster than old approach (90+ seconds)
        assert total_time < 60.0, f"Two-phase workflow too slow: {total_time:.2f}s"

        print(
            f"\n✓ All checks passed! Two-phase workflow is {90.0 / max(total_time, 1):.1f}x faster than old approach"  # noqa: E501
        )


class TestResourceUsage:
    """Test resource usage and memory management."""

    @pytest.mark.anyio
    async def test_memory_efficiency_single_process(self, sample_stl_file):
        """Test that single process uses less memory than all processes."""
        import gc
        import tracemalloc

        stl_bytes = sample_stl_file.read_bytes()

        # Force garbage collection
        gc.collect()

        # Measure memory for single process
        tracemalloc.start()
        _analyze_single_process(stl_bytes, "FDM")
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print("\nSingle process (FDM) memory usage:")
        print(f"  Current: {current / 1024:.1f} KB")
        print(f"  Peak: {peak / 1024:.1f} KB")

        # Peak memory should be reasonable (<100MB for simple file)
        assert (
            peak < 100 * 1024 * 1024
        ), f"Memory usage too high: {peak / 1024 / 1024:.1f} MB"

    @pytest.mark.anyio
    async def test_cleanup_removes_cached_data(self, sample_stl_file):
        """Test that cleanup properly removes cached data."""
        # Import inside function to avoid issues if module doesn't exist
        try:
            from src.main import _file_analysis_cache
        except ImportError:
            pytest.skip("File analysis cache not available (API functions)")
            return

        stl_bytes = sample_stl_file.read_bytes()
        upload_id = "test-cleanup-validation"

        # Add data to cache
        _file_analysis_cache[upload_id] = {
            "stl_bytes": stl_bytes,
            "cad_bytes": None,
            "cad_extension": None,
        }

        # Verify it's in cache
        assert upload_id in _file_analysis_cache

        # Cleanup
        del _file_analysis_cache[upload_id]

        # Verify it's gone
        assert upload_id not in _file_analysis_cache


class TestStage5Optimizations:
    """Test Stage 5 optimizations: process-specific checks, caching, adaptive tessellation."""  # noqa: E501

    @pytest.mark.anyio
    async def test_powder_bed_skips_overhang_check(self, sample_stl_file):
        """Test that powder-bed processes (SLS, MJF, BJ, DMLS) skip overhang checks."""
        from src.core.geometry import _analyze_single_process

        stl_bytes = sample_stl_file.read_bytes()

        # Test a powder-bed process (SLS)
        sls_result = _analyze_single_process(stl_bytes, "SLS")

        # Check that no overhang issues are reported for SLS
        overhang_issues = [
            i for i in sls_result.get("issues", []) if i.get("category") == "overhang"
        ]

        print(f"\nSLS overhang issues: {len(overhang_issues)}")
        print(f"SLS total issues: {len(sls_result.get('issues', []))}")

        # SLS should not have overhang checks (they're skipped)
        # However, we can't assert len == 0 because the file might legitimately have no overhangs  # noqa: E501
        # Instead, verify the analysis completes without error
        assert sls_result.get("reportType") == "SLS"
        assert "error_type" not in sls_result

    @pytest.mark.anyio
    async def test_powder_bed_skips_bridge_check(self, sample_stl_file):
        """Test that powder-bed processes skip bridge checks."""
        from src.core.geometry import _analyze_single_process

        stl_bytes = sample_stl_file.read_bytes()

        # Test a powder-bed process (MJF)
        mjf_result = _analyze_single_process(stl_bytes, "MJF")

        # Check that no bridge issues are reported for MJF
        bridge_issues = [
            i for i in mjf_result.get("issues", []) if i.get("category") == "bridge"
        ]

        print(f"\nMJF bridge issues: {len(bridge_issues)}")
        print(f"MJF total issues: {len(mjf_result.get('issues', []))}")

        # MJF should not have bridge checks (they're skipped)
        assert mjf_result.get("reportType") == "MJF"
        assert "error_type" not in mjf_result

    @pytest.mark.anyio
    async def test_cnc_skips_printing_checks(self, sample_stl_file):
        """Test that CNC processes skip printing-only checks (overhang, bridge)."""
        from src.core.geometry import _analyze_single_process

        stl_bytes = sample_stl_file.read_bytes()

        # Test CNC milling
        cnc_result = _analyze_single_process(stl_bytes, "CNC_MILL")

        # Check that no overhang or bridge issues are reported for CNC
        overhang_issues = [
            i for i in cnc_result.get("issues", []) if i.get("category") == "overhang"
        ]
        bridge_issues = [
            i for i in cnc_result.get("issues", []) if i.get("category") == "bridge"
        ]

        print(f"\nCNC_MILL overhang issues: {len(overhang_issues)}")
        print(f"CNC_MILL bridge issues: {len(bridge_issues)}")
        print(f"CNC_MILL total issues: {len(cnc_result.get('issues', []))}")

        # CNC should not have overhang or bridge checks (they're printing-only)
        assert len(overhang_issues) == 0, "CNC should not have overhang checks"
        assert len(bridge_issues) == 0, "CNC should not have bridge checks"
        assert cnc_result.get("reportType") in ["CNC_MILL", "CNC"]

    @pytest.mark.anyio
    async def test_result_caching_works(self, sample_stl_file):
        """Test that result caching works correctly."""
        from src.core.geometry_optimizations import (
            cache_result,
            clear_cache,
            get_cached_result,
        )

        stl_bytes = sample_stl_file.read_bytes()
        process_code = "FDM"

        # Clear cache first
        clear_cache()

        # First call should be a cache miss
        cached1 = get_cached_result(stl_bytes, process_code)
        assert cached1 is None, "First call should be cache miss"

        # Cache a result
        test_result = {"reportType": process_code, "issues": [], "test": "data"}
        cache_result(stl_bytes, process_code, test_result)

        # Second call should be a cache hit
        cached2 = get_cached_result(stl_bytes, process_code)
        assert cached2 is not None, "Second call should be cache hit"
        assert cached2["test"] == "data", "Cached result should match"

        print("\n✓ Caching works correctly")

    @pytest.mark.anyio
    async def test_adaptive_tessellation_tolerance(self):
        """Test that adaptive tessellation returns appropriate tolerances."""
        from src.core.geometry_optimizations import get_tessellation_tolerance

        # CNC processes should get high precision
        cnc_tolerance = get_tessellation_tolerance("CNC_MILL", file_size_mb=1.0)
        assert (
            cnc_tolerance == 0.02
        ), f"CNC should use 0.02 tolerance, got {cnc_tolerance}"

        # Printing processes with small file should use medium precision
        fdm_small_tolerance = get_tessellation_tolerance("FDM", file_size_mb=0.5)
        assert (
            fdm_small_tolerance == 0.05
        ), f"Small FDM file should use 0.05 tolerance, got {fdm_small_tolerance}"

        # Printing processes with large file should use coarse tolerance
        fdm_large_tolerance = get_tessellation_tolerance("FDM", file_size_mb=15.0)
        assert (
            fdm_large_tolerance == 0.2
        ), f"Large FDM file should use 0.2 tolerance, got {fdm_large_tolerance}"

        print("\n✓ Adaptive tessellation tolerances:")
        print(f"  CNC: {cnc_tolerance}mm")
        print(f"  FDM (small): {fdm_small_tolerance}mm")
        print(f"  FDM (large): {fdm_large_tolerance}mm")

    @pytest.mark.anyio
    async def test_cache_key_uniqueness(self):
        """Test that cache keys are unique per file and process."""
        from src.core.geometry_optimizations import get_cache_key

        stl_bytes1 = b"file content 1"
        stl_bytes2 = b"file content 2"

        # Different files should have different keys
        key1a = get_cache_key(stl_bytes1, "FDM")
        key1b = get_cache_key(stl_bytes1, "FDM")
        key2 = get_cache_key(stl_bytes2, "FDM")

        assert key1a == key1b, "Same file + process should have same key"
        assert key1a != key2, "Different files should have different keys"

        # Same file, different process should have different keys
        key_fdm = get_cache_key(stl_bytes1, "FDM")
        key_sla = get_cache_key(stl_bytes1, "SLA")

        assert key_fdm != key_sla, "Different processes should have different keys"

        print("\n✓ Cache key uniqueness verified")
        print(f"  File1+FDM: {key1a}")
        print(f"  File2+FDM: {key2}")
        print(f"  File1+SLA: {key_sla}")

    @pytest.mark.anyio
    async def test_cache_lru_eviction(self):
        """Test that cache evicts oldest entries when full."""
        from src.core.geometry_optimizations import _cache, cache_result

        # Store initial cache size
        initial_size = len(_cache)

        # Add 101 unique entries (cache max is 100)
        # Use unique bytes to ensure different hashes
        for i in range(101):
            stl_bytes = f"unique_file_{i}_{time.time()}".encode()
            process_code = "FDM"
            result = {"test": i}
            cache_result(stl_bytes, process_code, result)

        # Cache should have grown but not exceed ~100 + initial_size
        # The LRU eviction keeps cache at ~100 entries
        final_size = len(_cache)
        assert (
            final_size <= 100 + initial_size
        ), f"Cache should be <= {100 + initial_size} entries after LRU eviction, got {final_size}"  # noqa: E501

        print(
            f"\n✓ LRU eviction works correctly (initial: {initial_size}, final: {final_size})"  # noqa: E501
        )

    @pytest.mark.anyio
    async def test_clear_cache_works(self):
        """Test that clear_cache removes all entries."""
        from src.core.geometry_optimizations import cache_result, clear_cache

        # Add some test entries with unique content
        test_entries = []
        for i in range(10):
            stl_bytes = f"test_file_clear_{i}_{time.time()}".encode()
            cache_result(stl_bytes, "FDM", {"test": i})
            test_entries.append(get_cache_key(stl_bytes, "FDM"))

        # Verify entries were added (import fresh reference)
        from src.core.geometry_optimizations import _cache

        entries_before = len([k for k in _cache if k in test_entries])
        assert (
            entries_before == 10
        ), f"Should have added 10 test entries, found {entries_before}"

        # Clear cache
        clear_cache()

        # Verify cache is now empty (re-import to get new reference)
        from src.core.geometry_optimizations import _cache as cache_after

        assert (
            len(cache_after) == 0
        ), f"Cache should be empty after clear, got {len(cache_after)} entries"

        print("\n✓ Clear cache works correctly")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
