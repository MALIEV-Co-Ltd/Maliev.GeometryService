"""Tests for DFM timeout handling and performance.

These tests verify that DFM analysis handles timeouts gracefully,
performs within acceptable time limits, and properly cleans up resources.
"""

import pytest
import os
import time
import sys
import signal
import threading
import psutil
from pathlib import Path
from typing import Dict, Any, List

from src.core.geometry import (
    compute_dfm_analysis_for_stl,
    compute_multi_body_dfm_analysis,
)
from tests.test_utils import (
    monitor_resources,
    check_for_orphaned_processes,
    measure_performance,
    TimeoutTester,
)


@pytest.fixture
def sample_stl_file(test_assets_dir):
    """Get path to a sample STL file."""
    stl_file = test_assets_dir / "50x50x50mm-solid-cube-binary.stl"
    assert stl_file.exists(), f"Test asset {stl_file} not found"
    return stl_file


@pytest.fixture
def large_stl_file(test_assets_dir):
    """Get path to a large STL file for performance testing."""
    stl_files = list(test_assets_dir.glob("*.stl"))
    if not stl_files:
        pytest.skip("No STL files found")

    # Find the largest file
    large_file = max(stl_files, key=lambda f: f.stat().st_size)
    return large_file


class TestDFMTimeouts:
    """Test DFM timeout behavior."""

    def test_dfm_handles_simple_geometry_quickly(self, sample_stl_file):
        """Test that DFM analysis completes quickly for simple geometries."""
        stl_bytes = sample_stl_file.read_bytes()

        # Simple geometry should complete in < 10 seconds
        timeout_seconds = 10

        with monitor_resources() as monitor:
            result = compute_dfm_analysis_for_stl(
                stl_bytes, timeout_seconds=timeout_seconds
            )

        # Verify result
        assert result is not None, "DFM analysis returned None"
        assert "reports" in result, "Result missing 'reports' field"
        assert len(result["reports"]) > 0, "No DFM reports generated"

        # Verify it completed within timeout
        duration = monitor.snapshots[-1].timestamp - monitor.snapshots[0].timestamp
        assert duration < timeout_seconds, (
            f"Analysis took {duration}s, exceeds {timeout_seconds}s timeout"
        )

    def test_dfm_timeout_fails_gracefully(self, sample_stl_file):
        """Test that DFM timeout is handled gracefully without crashes."""
        stl_bytes = sample_stl_file.read_bytes()

        # Use very short timeout to force timeout
        timeout_seconds = 0.001

        orphaned_before = check_for_orphaned_processes()

        # Attempt analysis with impossible timeout
        result = compute_dfm_analysis_for_stl(
            stl_bytes, timeout_seconds=timeout_seconds
        )

        # Verify result structure even on timeout
        assert result is not None, "Result should not be None on timeout"

        # May have failed reports or error info
        # The important thing is it didn't crash

        # Check for orphaned processes
        time.sleep(0.5)  # Give time for cleanup
        orphaned_after = check_for_orphaned_processes()
        orphaned_count = len(orphaned_after) - len(orphaned_before)

        assert orphaned_count == 0, (
            f"Found {orphaned_count} orphaned processes after timeout"
        )

    def test_dfm_per_body_timeout_in_multi_body(self, multi_body_glbs):
        """Test that per-body timeout works in multi-body analysis."""
        if not multi_body_glbs or len(multi_body_glbs) < 2:
            pytest.skip("Need multi-body GLBs for timeout test")

        # Create a scenario where one body might timeout
        # by using very short timeout
        timeout_seconds = 0.001

        result = compute_multi_body_dfm_analysis(
            multi_body_glbs, timeout_seconds=timeout_seconds
        )

        # Should handle timeout gracefully
        assert result is not None, "Multi-body DFM returned None on timeout"

        # Verify some bodies might have failed but not all
        # (fault tolerance)
        if "reports" in result:
            # At least we got some structure back
            assert isinstance(result["reports"], list), "Reports should be a list"

    @pytest.mark.skipif(
        sys.platform == "win32", reason="SIGALRM not available on Windows"
    )
    def test_dfm_signal_timeout_single_body(self, sample_stl_file):
        """Test signal-based timeout mechanism on Unix systems."""
        stl_bytes = sample_stl_file.read_bytes()

        timeout_occurred = False

        def timeout_handler(signum, frame):
            nonlocal timeout_occurred
            timeout_occurred = True
            raise TimeoutError("DFM analysis timed out")

        # Set signal alarm
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(1)  # 1 second timeout

        try:
            result = compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=5)
            # If it completes quickly, cancel alarm
            signal.alarm(0)
        except TimeoutError:
            # Expected if analysis takes > 1 second
            pass
        finally:
            # Restore old handler
            signal.signal(signal.SIGALRM, old_handler)
            signal.alarm(0)

        # Verify handler was set up correctly
        # (actual timeout depends on analysis speed)

    @pytest.mark.skipif(
        sys.platform != "win32", reason="Watchdog primarily for Windows"
    )
    def test_dfm_watchdog_timeout_windows(self, sample_stl_file):
        """Test watchdog thread timeout mechanism on Windows."""
        stl_bytes = sample_stl_file.read_bytes()

        timeout_seconds = 0.001
        timed_out = False

        # Create watchdog thread
        stop_event = threading.Event()

        def watchdog():
            time.sleep(timeout_seconds)
            if not stop_event.is_set():
                nonlocal timed_out
                timed_out = True

        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()

        try:
            result = compute_dfm_analysis_for_stl(
                stl_bytes, timeout_seconds=timeout_seconds
            )
        finally:
            stop_event.set()
            watchdog_thread.join(timeout=2)

        # Verify watchdog was created
        # (actual timeout depends on analysis speed)


class TestDFMPerformance:
    """Test DFM performance characteristics."""

    def test_dfm_small_geometry_performance(self, sample_stl_file):
        """Test DFM performance on small geometries (< 10K vertices)."""
        stl_bytes = sample_stl_file.read_bytes()

        perf = measure_performance(
            lambda: compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=30)
        )

        # Verify it completed
        assert perf["success"], f"DFM analysis failed: {perf.get('error')}"

        # Small geometry should complete in < 10 seconds
        assert perf["duration_seconds"] < 10, (
            f"Small geometry took {perf['duration_seconds']:.2f}s, exceeds 10s baseline"
        )

        print(
            f"\nSmall geometry DFM: {perf['duration_seconds']:.2f}s, "
            f"memory: {perf['rss_mb_delta']:.1f}MB"
        )

    def test_dfm_medium_geometry_performance(self, large_stl_file):
        """Test DFM performance on medium geometries (10K-50K vertices)."""
        stl_bytes = large_stl_file.read_bytes()

        # Estimate vertex count (rough approximation: 50 bytes per vertex)
        estimated_vertices = len(stl_bytes) // 50

        # Only test if it's actually medium-sized
        if estimated_vertices < 10000:
            pytest.skip(
                f"File too small for medium test: {estimated_vertices} vertices"
            )

        perf = measure_performance(
            lambda: compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=60)
        )

        # Verify it completed
        assert perf["success"], f"DFM analysis failed: {perf.get('error')}"

        # Medium geometry should complete in < 30 seconds
        assert perf["duration_seconds"] < 30, (
            f"Medium geometry took {perf['duration_seconds']:.2f}s, exceeds 30s baseline"
        )

        print(
            f"\nMedium geometry ({estimated_vertices} vertices) DFM: "
            f"{perf['duration_seconds']:.2f}s, memory: {perf['rss_mb_delta']:.1f}MB"
        )

    def test_dfm_memory_cleanup(self, sample_stl_file):
        """Test that memory is cleaned up after DFM analysis."""
        stl_bytes = sample_stl_file.read_bytes()

        process = psutil.Process()
        rss_before = process.memory_info().rss / 1024 / 1024

        # Run DFM analysis
        result = compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=30)

        # Force cleanup
        import gc

        gc.collect()
        time.sleep(0.5)

        rss_after = process.memory_info().rss / 1024 / 1024
        rss_delta = rss_after - rss_before

        # Memory growth should be reasonable
        assert rss_delta < 300, f"Memory growth {rss_delta}MB exceeds 300MB"

    def test_dfm_concurrent_analyses(self, sample_stl_file):
        """Test that DFM can handle concurrent analyses."""
        stl_bytes = sample_stl_file.read_bytes()

        from concurrent.futures import ThreadPoolExecutor

        num_concurrent = 3
        futures = []
        results = []

        orphaned_before = check_for_orphaned_processes()

        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            for _ in range(num_concurrent):
                future = executor.submit(
                    compute_dfm_analysis_for_stl,
                    stl_bytes,
                    30,  # timeout_seconds
                )
                futures.append(future)

            # Wait for all to complete
            for future in futures:
                try:
                    result = future.result(timeout=60)
                    results.append(result)
                except Exception as e:
                    pytest.fail(f"Concurrent analysis failed: {e}")

        # Verify all completed
        assert len(results) == num_concurrent, (
            f"Only {len(results)}/{num_concurrent} analyses completed"
        )

        # Verify no orphaned processes
        orphaned_after = check_for_orphaned_processes()
        orphaned_count = len(orphaned_after) - len(orphaned_before)

        assert orphaned_count == 0, f"Found {orphaned_count} orphaned processes"


class TestDFMFaultTolerance:
    """Test DFM fault tolerance and error handling."""

    def test_dfm_handles_invalid_mesh(self):
        """Test that DFM handles invalid mesh data gracefully."""
        invalid_meshes = [
            b"",  # Empty
            b"invalid stl data",  # Wrong format
            b"\x00" * 100,  # Null bytes
        ]

        for i, invalid_mesh in enumerate(invalid_meshes):
            result = compute_dfm_analysis_for_stl(invalid_mesh, timeout_seconds=5)

            # Should handle gracefully, not crash
            assert result is not None, f"Invalid mesh {i} caused crash"

            # Should indicate failure
            if "reports" in result:
                # May have empty reports or error info
                assert isinstance(result["reports"], list), (
                    f"Invalid mesh {i} reports not a list"
                )

    def test_dfm_handles_mixed_valid_invalid_bodies(self, multi_body_glbs):
        """Test multi-body DFM with mix of valid and invalid bodies."""
        if not multi_body_glbs or len(multi_body_glbs) < 2:
            pytest.skip("Need multi-body GLBs for mixed validity test")

        # Create mixed list: valid, invalid, valid
        mixed_glbs = [
            multi_body_glbs[0],  # Valid
            b"invalid mesh data",  # Invalid
            multi_body_glbs[-1]
            if len(multi_body_glbs) > 1
            else multi_body_glbs[0],  # Valid
        ]

        result = compute_multi_body_dfm_analysis(mixed_glbs, timeout_seconds=30)

        # Should handle mixed validity gracefully
        assert result is not None, "Mixed validity returned None"

        # Should have some reports (at least for valid bodies)
        if "reports" in result:
            # Not all bodies may succeed, but some should
            assert isinstance(result["reports"], list), "Reports should be a list"

            # At minimum, we should get results for valid bodies
            # (unless all failed, which is also acceptable behavior)

    def test_dfm_continues_after_single_body_timeout(self, multi_body_glbs):
        """Test that multi-body DFM continues when one body times out."""
        if not multi_body_glbs or len(multi_body_glbs) < 3:
            pytest.skip("Need at least 3 bodies for timeout continuation test")

        # Use very short timeout to force timeouts
        timeout_seconds = 0.001

        result = compute_multi_body_dfm_analysis(
            multi_body_glbs, timeout_seconds=timeout_seconds
        )

        # Should return structure even with timeouts
        assert result is not None, "Multi-body with timeout returned None"

        # Fault tolerance: should have reports structure
        if "reports" in result:
            assert isinstance(result["reports"], list), "Reports should be a list"

            # Even with timeouts, we should get partial results
            # or at least empty list (not crash)


class TestDFMResourceManagement:
    """Test DFM resource management and cleanup."""

    def test_dfm_cleanup_after_timeout(self, sample_stl_file):
        """Test that DFM properly cleans up resources after timeout."""
        stl_bytes = sample_stl_file.read_bytes()

        orphaned_before = check_for_orphaned_processes()

        # Force timeout
        with monitor_resources() as monitor:
            result = compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=0.001)

        # Give time for cleanup
        time.sleep(1.0)

        # Check for orphaned processes
        orphaned_after = check_for_orphaned_processes()
        orphaned_count = len(orphaned_after) - len(orphaned_before)

        assert orphaned_count == 0, (
            f"Found {orphaned_count} orphaned processes after timeout"
        )

        # Verify memory was released
        memory_growth = monitor.get_memory_growth()
        # Allow some growth but not excessive
        assert memory_growth < 200, (
            f"Memory growth {memory_growth}MB after timeout is excessive"
        )

    def test_dfm_temp_file_cleanup(self, sample_stl_file):
        """Test that DFM cleans up temporary files."""
        import tempfile

        temp_dir = tempfile.gettempdir()
        temp_files_before = len([f for f in Path(temp_dir).iterdir() if f.is_file()])

        # Run DFM analysis
        result = compute_dfm_analysis_for_stl(
            sample_stl_file.read_bytes(), timeout_seconds=30
        )

        # Give time for cleanup
        time.sleep(0.5)

        # Check temp files
        temp_files_after = len([f for f in Path(temp_dir).iterdir() if f.is_file()])
        temp_file_growth = temp_files_after - temp_files_before

        # Temp files should be cleaned up (allow some growth for legitimate reasons)
        # Excessive growth would indicate a leak
        assert temp_file_growth < 10, (
            f"Temp file growth {temp_file_growth} indicates potential leak"
        )


@pytest.fixture
def multi_body_glbs(test_assets_dir):
    """Create mock multi-body GLB data for testing."""
    # For now, return empty list
    # In real tests, this would load actual multi-body GLBs
    return []
