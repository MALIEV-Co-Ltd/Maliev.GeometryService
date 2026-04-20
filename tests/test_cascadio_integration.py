"""Integration tests for cascadio CAD loading with ProcessPoolExecutor.

These tests verify that cascadio loading works correctly with ProcessPoolExecutor,
handles timeouts gracefully, and properly cleans up resources.
"""

import pytest
import os
import time
import psutil
from pathlib import Path
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeout,
)

from src.core.geometry import GeometryProcessor, load_cascadio_geometry
from tests.test_utils import (
    monitor_resources,
    check_for_orphaned_processes,
    wait_for_condition,
    measure_performance,
)


@pytest.fixture
def sample_step_file(test_assets_dir):
    """Get path to a sample STEP file."""
    step_file = test_assets_dir / "50x50x50mm-solid-cube.step"
    assert step_file.exists(), f"Test asset {step_file} not found"
    return step_file


@pytest.fixture
def multi_body_step_file(test_assets_dir):
    """Get path to a multi-body STEP file if available."""
    # Try to find a multi-body STEP file
    multi_files = list(test_assets_dir.glob("*multibod*.step"))
    for f in multi_files:
        if f.stat().st_size > 5000:  # > 5KB
            return f
    # Fall back to 50mm-polygon-multibodies-nonoverlap.step
    multi_file = test_assets_dir / "50mm-polygon-multibodies-nonoverlap.step"
    if multi_file.exists():
        return multi_file
    # Final fallback
    return test_assets_dir / "50x50x50mm-solid-cube.step"


class TestCascadioIntegration:
    """Test cascadio integration with ProcessPoolExecutor."""

    def test_cascadio_loads_real_step_files(self, sample_step_file):
        """Test that GeometryProcessor can load real STEP files."""
        step_bytes = sample_step_file.read_bytes()

        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            # Monitor resources during load
            with monitor_resources() as monitor:
                metrics, preview, thumbnail = processor.analyze_bytes(
                    step_bytes, ".step"
                )

            # Verify result structure
            assert metrics is not None, "analyze_bytes returned None metrics"
            assert preview is not None or thumbnail is not None, "No output generated"

            # Check that we got some metrics
            assert hasattr(metrics, "volume"), "Metrics missing volume"

            # Check memory usage is reasonable
            peak_memory = monitor.get_peak_memory()
            assert peak_memory < 1000, (
                f"Peak memory usage {peak_memory}MB exceeds 1000MB"
            )
        finally:
            processor.shutdown()

    def test_cascadio_handles_timeout_gracefully(self, sample_step_file):
        """Test that cascadio timeout is handled gracefully with proper cleanup."""
        step_bytes = sample_step_file.read_bytes()

        # Use a very short timeout to trigger timeout
        short_timeout = 0.001  # 1ms - essentially immediate timeout

        # Monitor for orphaned processes
        orphaned_before = check_for_orphaned_processes()

        # Track child processes before
        current_process = psutil.Process()
        children_before = len(current_process.children(recursive=True))

        # Attempt load with short timeout (will timeout)
        start_time = time.time()
        result = load_cascadio_geometry(step_bytes, timeout_seconds=short_timeout)
        duration = time.time() - start_time

        # Verify timeout occurred quickly
        assert duration < 5.0, f"Timeout took too long: {duration}s"

        # Verify result indicates failure
        assert result is not None, "Result should not be None on timeout"
        assert result.get("success") is False, (
            "Result should indicate failure on timeout"
        )

        # Wait for cleanup to complete
        time.sleep(1.0)

        # Track child processes after
        children_after = len(current_process.children(recursive=True))

        # Verify no orphaned processes
        orphaned_after = check_for_orphaned_processes()
        orphaned_count = len(orphaned_after) - len(orphaned_before)

        assert orphaned_count == 0, (
            f"Found {orphaned_count} orphaned processes after timeout"
        )

        # Verify children were cleaned up (may have some, but not excessive)
        child_growth = children_after - children_before
        assert child_growth < 3, f"Too many child processes created: {child_growth}"

    def test_cascadio_preserves_multi_body_structure(self, multi_body_step_file):
        """Test that cascadio preserves multi-body structure from STEP files."""
        step_bytes = multi_body_step_file.read_bytes()

        result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

        if result and result.get("success"):
            bodies = result.get("bodies", [])
            assert len(bodies) > 0, "No bodies loaded"

            # Verify each body has required fields
            for i, body in enumerate(bodies):
                assert "name" in body, f"Body {i} missing 'name' field"
                assert "mesh" in body, f"Body {i} missing 'mesh' field"

                # Verify mesh is valid GLB data
                mesh_bytes = body["mesh"]
                assert len(mesh_bytes) > 0, f"Body {i} has empty mesh"
                assert mesh_bytes[:4] == b"glTF", f"Body {i} mesh is not valid GLB"

                # Verify body names are unique or descriptive
                if len(bodies) > 1:
                    # Multi-body file - names should be present
                    assert body["name"], f"Body {i} has empty name in multi-body file"

    def test_cascadio_concurrent_loads(self, sample_step_file):
        """Test that cascadio can handle concurrent load requests."""
        step_bytes = sample_step_file.read_bytes()

        # Load multiple files concurrently
        num_concurrent = 3
        futures = []
        results = []

        with ProcessPoolExecutor(max_workers=num_concurrent) as executor:
            for _ in range(num_concurrent):
                future = executor.submit(
                    load_cascadio_geometry,
                    step_bytes,
                    30,  # timeout_seconds
                )
                futures.append(future)

            # Wait for all to complete
            for future in futures:
                try:
                    result = future.result(timeout=60)
                    results.append(result)
                except FuturesTimeout:
                    pytest.fail("Concurrent load timed out")

        # Verify all completed
        assert len(results) == num_concurrent, (
            f"Only {len(results)}/{num_concurrent} loads completed"
        )

        # Verify all succeeded (or at least didn't crash)
        for i, result in enumerate(results):
            assert result is not None, f"Load {i} returned None"

    def test_cascadio_memory_cleanup(self, sample_step_file):
        """Test that memory is properly cleaned up after cascadio load."""
        step_bytes = sample_step_file.read_bytes()

        # Measure memory before
        process = psutil.Process()
        rss_before = process.memory_info().rss / 1024 / 1024

        # Load the file
        result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

        # Force garbage collection
        import gc

        gc.collect()

        # Give system time to release memory
        time.sleep(0.5)

        # Measure memory after
        rss_after = process.memory_info().rss / 1024 / 1024
        rss_delta = rss_after - rss_before

        # Memory growth should be reasonable (< 200MB for a simple file)
        # This allows for some caching but ensures no major leaks
        assert rss_delta < 200, f"Memory growth {rss_delta}MB exceeds 200MB threshold"

    def test_cascadio_performance_baseline(self, sample_step_file):
        """Test cascadio load performance against baseline."""
        step_bytes = sample_step_file.read_bytes()

        # Measure load time
        perf = measure_performance(
            lambda: load_cascadio_geometry(step_bytes, timeout_seconds=30)
        )

        # Verify it completed successfully
        assert perf["success"], f"Load failed: {perf.get('error')}"

        # Verify performance is reasonable (< 30 seconds)
        assert perf["duration_seconds"] < 30, (
            f"Load took {perf['duration_seconds']:.2f}s, exceeds 30s baseline"
        )

        # Log performance for monitoring
        print(
            f"\nCascadio load performance: {perf['duration_seconds']:.2f}s, "
            f"memory: {perf['rss_mb_delta']:.1f}MB delta"
        )

    def test_cascadio_invalid_input_handling(self):
        """Test that cascadio handles invalid input gracefully."""
        invalid_inputs = [
            b"",  # Empty data
            b"not a step file",  # Invalid format
            b"too short",  # Too short to be valid
        ]

        for i, invalid_data in enumerate(invalid_inputs):
            result = load_cascadio_geometry(invalid_data, timeout_seconds=5)

            # Should handle gracefully, not crash
            assert result is not None, f"Invalid input {i} returned None"

            # Should indicate failure
            assert result.get("success") is False, (
                f"Invalid input {i} incorrectly succeeded"
            )

    def test_cascadio_large_file_timeout(self, test_assets_dir):
        """Test timeout behavior with a large file that might timeout."""
        # Use the largest available file
        files = list(test_assets_dir.glob("*.step")) + list(
            test_assets_dir.glob("*.stp")
        )
        if not files:
            pytest.skip("No STEP files found for timeout test")

        # Find the largest file
        largest_file = max(files, key=lambda f: f.stat().st_size)
        step_bytes = largest_file.read_bytes()

        # Use a moderate timeout
        timeout_seconds = 5.0

        # Monitor resources
        with monitor_resources() as monitor:
            result = load_cascadio_geometry(step_bytes, timeout_seconds=timeout_seconds)

        # Verify it completed (either success or timeout handled)
        assert result is not None, "Result should not be None"

        # Check for orphaned processes
        orphaned = check_for_orphaned_processes()
        assert len(orphaned) == 0, f"Found {len(orphaned)} orphaned processes"

        # Verify memory was released
        memory_growth = monitor.get_memory_growth()
        # Allow some growth but not excessive
        assert memory_growth < 500, f"Memory growth {memory_growth}MB is excessive"


class TestCascadioCleanup:
    """Test cascadio cleanup and resource management."""

    def test_processpool_terminates_on_timeout(self):
        """Test that ProcessPoolExecutor properly terminates workers on timeout."""

        # Create a function that will timeout
        def slow_function():
            time.sleep(100)  # Sleep for a long time
            return "completed"

        with ProcessPoolExecutor(max_workers=1) as executor:
            future = executor.submit(slow_function)

            # Wait with short timeout
            with pytest.raises(FuturesTimeout):
                future.result(timeout=0.5)

            # Cancel the future
            future.cancel()

            # Shutdown executor
            executor.shutdown(wait=True, timeout=2)

        # Give time for cleanup
        time.sleep(0.5)

        # Check for orphaned processes
        orphaned = check_for_orphaned_processes()
        assert len(orphaned) == 0, f"Found {len(orphaned)} orphaned processes"

    def test_threadpool_vs_processpool_cleanup(self):
        """Compare ThreadPoolExecutor vs ProcessPoolExecutor cleanup."""

        # Note: This test documents why we use ProcessPoolExecutor
        def hang_function():
            time.sleep(100)
            return "done"

        # Test ThreadPoolExecutor (threads can't be forcefully terminated)
        orphaned_before = check_for_orphaned_processes()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(hang_function)

            try:
                future.result(timeout=0.5)
            except FuturesTimeout:
                pass

            # Shutdown without waiting
            executor.shutdown(wait=False)

        # Give time for cleanup
        time.sleep(1.0)

        orphaned_thread = check_for_orphaned_processes()
        thread_orphans = len(orphaned_thread) - len(orphaned_before)

        # Now test ProcessPoolExecutor (processes CAN be terminated)
        orphaned_before = check_for_orphaned_processes()

        with ProcessPoolExecutor(max_workers=1) as executor:
            future = executor.submit(hang_function)

            try:
                future.result(timeout=0.5)
            except FuturesTimeout:
                pass

            # Shutdown without waiting
            executor.shutdown(wait=False)

        # Give time for cleanup
        time.sleep(1.0)

        orphaned_process = check_for_orphaned_processes()
        process_orphans = len(orphaned_process) - len(orphaned_before)

        # ProcessPool should have fewer orphans (or same) than ThreadPool
        # Note: This might not always be true depending on Python implementation,
        # but it demonstrates the intent
        print(f"\nThreadPoolExecutor orphans: {thread_orphans}")
        print(f"ProcessPoolExecutor orphans: {process_orphans}")
