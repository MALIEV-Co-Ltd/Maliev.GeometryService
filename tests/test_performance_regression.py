"""Performance regression tests for DFM and cascadio operations.

These tests establish performance baselines and flag regressions.
Tests are designed to be repeatable and provide actionable metrics.
"""

import pytest
import time
import psutil
from pathlib import Path
from typing import Dict, Any, List

from src.core.geometry import (
    load_cascadio_geometry,
    compute_dfm_analysis_for_stl,
)
from tests.test_utils import (
    monitor_resources,
    measure_performance,
    check_for_orphaned_processes,
)


# Performance thresholds (in seconds)
CASCADIO_LOAD_TIMEOUTS = {
    "tiny": 5.0,      # < 100KB
    "small": 10.0,    # 100KB - 1MB
    "medium": 20.0,   # 1MB - 5MB
    "large": 30.0,    # 5MB - 10MB
    "huge": 60.0,     # > 10MB
}

DFM_ANALYSIS_TIMEOUTS = {
    "tiny": 5.0,      # < 1K vertices
    "small": 10.0,    # 1K - 10K vertices
    "medium": 30.0,   # 10K - 50K vertices
    "large": 60.0,    # 50K - 100K vertices
    "huge": 90.0,     # > 100K vertices
}

MEMORY_THRESHOLDS_MB = {
    "cascadio_load": 500,
    "dfm_analysis": 300,
}


class TestCascadioPerformance:
    """Performance tests for cascadio CAD loading."""

    def test_cascadio_performance_baseline_tiny(self, test_assets_dir):
        """Test cascadio load performance for tiny files (< 100KB)."""
        step_files = list(test_assets_dir.glob("*.step")) + list(test_assets_dir.glob("*.stp"))

        # Find tiny file
        tiny_file = None
        for f in step_files:
            size = f.stat().st_size
            if size < 100 * 1024:  # < 100KB
                tiny_file = f
                break

        if not tiny_file:
            pytest.skip("No tiny STEP file found")

        step_bytes = tiny_file.read_bytes()

        perf = measure_performance(
            lambda: load_cascadio_geometry(step_bytes, timeout_seconds=30)
        )

        # Verify success
        assert perf["success"], f"Load failed: {perf.get('error')}"

        # Check performance
        threshold = CASCADIO_LOAD_TIMEOUTS["tiny"]
        assert perf["duration_seconds"] < threshold, \
            f"Tiny file load took {perf['duration_seconds']:.2f}s, exceeds {threshold}s threshold"

        # Check memory
        assert perf["rss_mb_delta"] < MEMORY_THRESHOLDS_MB["cascadio_load"], \
            f"Memory usage {perf['rss_mb_delta']:.1f}MB exceeds threshold"

        print(f"\nTiny file ({len(step_bytes)} bytes): {perf['duration_seconds']:.2f}s, "
              f"{perf['rss_mb_delta']:.1f}MB")

    def test_cascadio_performance_baseline_small(self, test_assets_dir):
        """Test cascadio load performance for small files (100KB - 1MB)."""
        step_files = list(test_assets_dir.glob("*.step")) + list(test_assets_dir.glob("*.stp"))

        # Find small file
        small_file = None
        for f in step_files:
            size = f.stat().st_size
            if 100 * 1024 <= size < 1024 * 1024:  # 100KB - 1MB
                small_file = f
                break

        if not small_file:
            pytest.skip("No small STEP file found")

        step_bytes = small_file.read_bytes()

        perf = measure_performance(
            lambda: load_cascadio_geometry(step_bytes, timeout_seconds=30)
        )

        # Verify success
        assert perf["success"], f"Load failed: {perf.get('error')}"

        # Check performance
        threshold = CASCADIO_LOAD_TIMEOUTS["small"]
        assert perf["duration_seconds"] < threshold, \
            f"Small file load took {perf['duration_seconds']:.2f}s, exceeds {threshold}s threshold"

        # Check memory
        assert perf["rss_mb_delta"] < MEMORY_THRESHOLDS_MB["cascadio_load"], \
            f"Memory usage {perf['rss_mb_delta']:.1f}MB exceeds threshold"

        print(f"\nSmall file ({len(step_bytes)} bytes): {perf['duration_seconds']:.2f}s, "
              f"{perf['rss_mb_delta']:.1f}MB")

    def test_cascadio_performance_regression(self, test_assets_dir):
        """Test for cascadio performance regression over time."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Run multiple times to get stable measurement
        times = []
        for _ in range(3):
            perf = measure_performance(
                lambda: load_cascadio_geometry(step_bytes, timeout_seconds=30)
            )
            assert perf["success"], f"Load failed: {perf.get('error')}"
            times.append(perf["duration_seconds"])

        # Calculate average
        avg_time = sum(times) / len(times)

        # Should complete in reasonable time
        assert avg_time < 15.0, \
            f"Average load time {avg_time:.2f}s indicates performance regression"

        # Variance should be low (consistent performance)
        variance = max(times) - min(times)
        assert variance < 5.0, \
            f"High variance {variance:.2f}s indicates inconsistent performance"

        print(f"\nCascadio load times: {times}")
        print(f"Average: {avg_time:.2f}s, variance: {variance:.2f}s")


class TestDFMPerformance:
    """Performance tests for DFM analysis."""

    def estimate_vertex_count(self, stl_bytes: bytes) -> int:
        """Estimate vertex count from STL file size."""
        # Rough estimate: 50 bytes per vertex
        return len(stl_bytes) // 50

    def test_dfm_performance_baseline_tiny(self, test_assets_dir):
        """Test DFM performance for tiny meshes (< 1K vertices)."""
        stl_files = list(test_assets_dir.glob("*.stl"))

        # Find tiny file
        tiny_file = None
        for f in stl_files:
            stl_bytes = f.read_bytes()
            vertices = self.estimate_vertex_count(stl_bytes)
            if vertices < 1000:
                tiny_file = f
                break

        if not tiny_file:
            pytest.skip("No tiny STL file found")

        stl_bytes = tiny_file.read_bytes()

        perf = measure_performance(
            lambda: compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=30)
        )

        # Verify success
        assert perf["success"], f"DFM failed: {perf.get('error')}"

        # Check performance
        threshold = DFM_ANALYSIS_TIMEOUTS["tiny"]
        assert perf["duration_seconds"] < threshold, \
            f"Tiny mesh DFM took {perf['duration_seconds']:.2f}s, exceeds {threshold}s threshold"

        # Check memory
        assert perf["rss_mb_delta"] < MEMORY_THRESHOLDS_MB["dfm_analysis"], \
            f"Memory usage {perf['rss_mb_delta']:.1f}MB exceeds threshold"

        vertices = self.estimate_vertex_count(stl_bytes)
        print(f"\nTiny mesh (~{vertices} vertices): {perf['duration_seconds']:.2f}s, "
              f"{perf['rss_mb_delta']:.1f}MB")

    def test_dfm_performance_baseline_small(self, test_assets_dir):
        """Test DFM performance for small meshes (1K - 10K vertices)."""
        stl_files = list(test_assets_dir.glob("*.stl"))

        # Find small file
        small_file = None
        for f in stl_files:
            stl_bytes = f.read_bytes()
            vertices = self.estimate_vertex_count(stl_bytes)
            if 1000 <= vertices < 10000:
                small_file = f
                break

        if not small_file:
            pytest.skip("No small STL file found")

        stl_bytes = small_file.read_bytes()

        perf = measure_performance(
            lambda: compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=30)
        )

        # Verify success
        assert perf["success"], f"DFM failed: {perf.get('error')}"

        # Check performance
        threshold = DFM_ANALYSIS_TIMEOUTS["small"]
        assert perf["duration_seconds"] < threshold, \
            f"Small mesh DFM took {perf['duration_seconds']:.2f}s, exceeds {threshold}s threshold"

        vertices = self.estimate_vertex_count(stl_bytes)
        print(f"\nSmall mesh (~{vertices} vertices): {perf['duration_seconds']:.2f}s, "
              f"{perf['rss_mb_delta']:.1f}MB")

    def test_dfm_performance_baseline_medium(self, test_assets_dir):
        """Test DFM performance for medium meshes (10K - 50K vertices)."""
        stl_files = list(test_assets_dir.glob("*.stl"))

        # Find medium file
        medium_file = None
        for f in stl_files:
            stl_bytes = f.read_bytes()
            vertices = self.estimate_vertex_count(stl_bytes)
            if 10000 <= vertices < 50000:
                medium_file = f
                break

        if not medium_file:
            pytest.skip("No medium STL file found")

        stl_bytes = medium_file.read_bytes()

        perf = measure_performance(
            lambda: compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=60)
        )

        # Verify success
        assert perf["success"], f"DFM failed: {perf.get('error')}"

        # Check performance
        threshold = DFM_ANALYSIS_TIMEOUTS["medium"]
        assert perf["duration_seconds"] < threshold, \
            f"Medium mesh DFM took {perf['duration_seconds']:.2f}s, exceeds {threshold}s threshold"

        vertices = self.estimate_vertex_count(stl_bytes)
        print(f"\nMedium mesh (~{vertices} vertices): {perf['duration_seconds']:.2f}s, "
              f"{perf['rss_mb_delta']:.1f}MB")

    def test_dfm_performance_regression(self, test_assets_dir):
        """Test for DFM performance regression over time."""
        stl_file = test_assets_dir / "cube.stl"
        if not stl_file.exists():
            pytest.skip("cube.stl not found")

        stl_bytes = stl_file.read_bytes()

        # Run multiple times to get stable measurement
        times = []
        for _ in range(3):
            perf = measure_performance(
                lambda: compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=30)
            )
            assert perf["success"], f"DFM failed: {perf.get('error')}"
            times.append(perf["duration_seconds"])

        # Calculate average
        avg_time = sum(times) / len(times)

        # Should complete in reasonable time
        assert avg_time < 10.0, \
            f"Average DFM time {avg_time:.2f}s indicates performance regression"

        # Variance should be low
        variance = max(times) - min(times)
        assert variance < 3.0, \
            f"High variance {variance:.2f}s indicates inconsistent performance"

        print(f"\nDFM times: {times}")
        print(f"Average: {avg_time:.2f}s, variance: {variance:.2f}s")


class TestMemoryCleanup:
    """Test memory cleanup and leak detection."""

    def test_memory_cleanup_after_cascadio_load(self, test_assets_dir):
        """Test that memory is cleaned up after cascadio load."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        process = psutil.Process()
        rss_baseline = process.memory_info().rss / 1024 / 1024

        # Perform multiple loads
        for _ in range(3):
            result = load_cascadio_geometry(step_bytes, timeout_seconds=30)
            assert result is not None, "Load returned None"

        # Force cleanup
        import gc
        gc.collect()
        time.sleep(0.5)

        # Check memory returned to baseline
        rss_final = process.memory_info().rss / 1024 / 1024
        rss_growth = rss_final - rss_baseline

        # Growth should be modest (some caching is acceptable)
        assert rss_growth < 200, \
            f"Memory growth {rss_growth:.1f}MB after multiple loads indicates leak"

        print(f"\nMemory growth after 3 loads: {rss_growth:.1f}MB")

    def test_memory_cleanup_after_dfm_analysis(self, test_assets_dir):
        """Test that memory is cleaned up after DFM analysis."""
        stl_file = test_assets_dir / "cube.stl"
        if not stl_file.exists():
            pytest.skip("cube.stl not found")

        stl_bytes = stl_file.read_bytes()

        process = psutil.Process()
        rss_baseline = process.memory_info().rss / 1024 / 1024

        # Perform multiple analyses
        for _ in range(3):
            result = compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=30)
            assert result is not None, "DFM returned None"

        # Force cleanup
        import gc
        gc.collect()
        time.sleep(0.5)

        # Check memory returned to baseline
        rss_final = process.memory_info().rss / 1024 / 1024
        rss_growth = rss_final - rss_baseline

        # Growth should be modest
        assert rss_growth < 150, \
            f"Memory growth {rss_growth:.1f}MB after multiple DFM analyses indicates leak"

        print(f"\nMemory growth after 3 DFM analyses: {rss_growth:.1f}MB")

    def test_no_orphaned_processes_after_load(self, test_assets_dir):
        """Test that no orphaned processes remain after cascadio load."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        orphaned_before = check_for_orphaned_processes()

        # Perform load
        result = load_cascadio_geometry(step_bytes, timeout_seconds=30)
        assert result is not None, "Load returned None"

        # Wait for cleanup
        time.sleep(1.0)

        # Check for orphaned processes
        orphaned_after = check_for_orphaned_processes()
        orphaned_count = len(orphaned_after) - len(orphaned_before)

        assert orphaned_count == 0, \
            f"Found {orphaned_count} orphaned processes after load"

    def test_no_orphaned_processes_after_dfm(self, test_assets_dir):
        """Test that no orphaned processes remain after DFM analysis."""
        stl_file = test_assets_dir / "cube.stl"
        if not stl_file.exists():
            pytest.skip("cube.stl not found")

        stl_bytes = stl_file.read_bytes()

        orphaned_before = check_for_orphaned_processes()

        # Perform DFM analysis
        result = compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=30)
        assert result is not None, "DFM returned None"

        # Wait for cleanup
        time.sleep(1.0)

        # Check for orphaned processes
        orphaned_after = check_for_orphaned_processes()
        orphaned_count = len(orphaned_after) - len(orphaned_before)

        assert orphaned_count == 0, \
            f"Found {orphaned_count} orphaned processes after DFM"


class TestResourceMonitoring:
    """Test resource monitoring during operations."""

    def test_cascadio_resource_profile(self, test_assets_dir):
        """Profile resource usage during cascadio load."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        with monitor_resources() as monitor:
            result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

        assert result is not None, "Load returned None"

        # Analyze snapshots
        peak_memory = monitor.get_peak_memory()
        final_memory = monitor.get_final_memory()
        memory_growth = monitor.get_memory_growth()

        print(f"\nCascadio resource profile:")
        print(f"  Peak memory: {peak_memory:.1f}MB")
        print(f"  Final memory: {final_memory:.1f}MB")
        print(f"  Memory growth: {memory_growth:.1f}MB")
        print(f"  Snapshots: {len(monitor.snapshots)}")

        # Verify reasonable resource usage
        assert peak_memory < 1000, f"Peak memory {peak_memory:.1f}MB is excessive"
        assert not monitor.has_orphaned_children(), "Orphaned child processes detected"

    def test_dfm_resource_profile(self, test_assets_dir):
        """Profile resource usage during DFM analysis."""
        stl_file = test_assets_dir / "cube.stl"
        if not stl_file.exists():
            pytest.skip("cube.stl not found")

        stl_bytes = stl_file.read_bytes()

        with monitor_resources() as monitor:
            result = compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=30)

        assert result is not None, "DFM returned None"

        # Analyze snapshots
        peak_memory = monitor.get_peak_memory()
        final_memory = monitor.get_final_memory()
        memory_growth = monitor.get_memory_growth()

        print(f"\nDFM resource profile:")
        print(f"  Peak memory: {peak_memory:.1f}MB")
        print(f"  Final memory: {final_memory:.1f}MB")
        print(f"  Memory growth: {memory_growth:.1f}MB")
        print(f"  Snapshots: {len(monitor.snapshots)}")

        # Verify reasonable resource usage
        assert peak_memory < 800, f"Peak memory {peak_memory:.1f}MB is excessive"
        assert not monitor.has_orphaned_children(), "Orphaned child processes detected"
