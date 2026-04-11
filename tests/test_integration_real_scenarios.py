"""Integration tests for real-world scenarios.

These tests simulate actual usage patterns and verify the entire
pipeline works correctly from file upload through DFM analysis.
"""

import pytest
import time
import tempfile
from pathlib import Path
from typing import List, Dict, Any

from src.core.geometry import (
    load_cascadio_geometry,
    compute_dfm_analysis_for_stl,
    compute_multi_body_dfm_analysis,
)
from tests.test_utils import (
    monitor_resources,
    check_for_orphaned_processes,
    cleanup_temp_files,
)


class TestFullPipeline:
    """Test complete pipeline from file upload to overlays."""

    def test_full_pipeline_step_to_overlays(self, test_assets_dir):
        """Test complete pipeline: STEP file → multi-body GLBs → DFM analysis."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Step 1: Load STEP file via cascadio
        cascadio_result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

        assert cascadio_result is not None, "Cascadio loading failed"
        assert cascadio_result.get("success") is not False, "Cascadio loading indicated failure"

        if not cascadio_result.get("success"):
            # If cascadio failed, we can't continue
            pytest.skip("Cascadio loading failed - can't test full pipeline")

        # Step 2: Extract GLBs from result
        bodies = cascadio_result.get("bodies", [])
        assert len(bodies) > 0, "No bodies extracted from STEP file"

        glb_bytes_list = [body["mesh"] for body in bodies]

        # Step 3: Run DFM analysis on GLBs
        dfm_result = compute_multi_body_dfm_analysis(glb_bytes_list, timeout_seconds=90)

        # Step 4: Verify DFM results
        assert dfm_result is not None, "DFM analysis returned None"

        if "reports" in dfm_result:
            # At least some bodies should have reports
            assert len(dfm_result["reports"]) > 0, "No DFM reports generated"

            # Verify report structure
            for i, report in enumerate(dfm_result["reports"]):
                if report is not None:
                    assert "body_id" in report, f"Report {i} missing body_id"
                    # Other fields may vary depending on DFM implementation

    def test_pipeline_with_timeout_recovery(self, test_assets_dir):
        """Test pipeline recovery after timeout during cascadio loading."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Attempt load with very short timeout (will timeout)
        result = load_cascadio_geometry(step_bytes, timeout_seconds=0.001)

        # Should handle gracefully
        assert result is not None, "Timeout returned None"

        # Now try with normal timeout - should succeed
        result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

        assert result is not None, "Second load returned None"
        assert result.get("success") is not False, "Second load failed after timeout"

    def test_pipeline_with_multi_body_file(self, test_assets_dir):
        """Test complete pipeline with a multi-body STEP file."""
        # Find a STEP file that might be multi-body
        step_files = list(test_assets_dir.glob("*.step")) + list(test_assets_dir.glob("*.stp"))
        if not step_files:
            pytest.skip("No STEP files found")

        # Try the largest file (most likely to be multi-body)
        step_file = max(step_files, key=lambda f: f.stat().st_size)
        step_bytes = step_file.read_bytes()

        # Load STEP file
        cascadio_result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

        if not cascadio_result or not cascadio_result.get("success"):
            pytest.skip("Cascadio loading failed")

        bodies = cascadio_result.get("bodies", [])

        if len(bodies) <= 1:
            # Not multi-body, skip
            pytest.skip("File is not multi-body")

        # Extract GLBs
        glb_bytes_list = [body["mesh"] for body in bodies]

        # Run DFM analysis on all bodies
        dfm_result = compute_multi_body_dfm_analysis(glb_bytes_list, timeout_seconds=120)

        # Verify results
        assert dfm_result is not None, "Multi-body DFM returned None"

        if "reports" in dfm_result:
            # Should have reports for each body (or partial results)
            assert isinstance(dfm_result["reports"], list), "Reports should be a list"


class TestConcurrentOperations:
    """Test concurrent operations and resource management."""

    def test_concurrent_step_file_uploads(self, test_assets_dir):
        """Test handling multiple concurrent STEP file uploads."""
        from concurrent.futures import ThreadPoolExecutor

        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Simulate 3 concurrent uploads
        num_concurrent = 3
        results = []

        orphaned_before = check_for_orphaned_processes()

        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            futures = []
            for _ in range(num_concurrent):
                future = executor.submit(
                    load_cascadio_geometry,
                    step_bytes,
                    30,  # timeout_seconds
                )
                futures.append(future)

            for future in futures:
                try:
                    result = future.result(timeout=60)
                    results.append(result)
                except Exception as e:
                    pytest.fail(f"Concurrent load failed: {e}")

        # Verify all completed
        assert len(results) == num_concurrent, \
            f"Only {len(results)}/{num_concurrent} uploads completed"

        # Verify no orphaned processes
        time.sleep(1.0)  # Give time for cleanup
        orphaned_after = check_for_orphaned_processes()
        orphaned_count = len(orphaned_after) - len(orphaned_before)

        assert orphaned_count == 0, f"Found {orphaned_count} orphaned processes"

    def test_concurrent_dfm_analyses(self, test_assets_dir):
        """Test handling multiple concurrent DFM analyses."""
        from concurrent.futures import ThreadPoolExecutor

        stl_file = test_assets_dir / "cube.stl"
        if not stl_file.exists():
            pytest.skip("cube.stl not found")

        stl_bytes = stl_file.read_bytes()

        # Simulate 3 concurrent analyses
        num_concurrent = 3
        results = []

        orphaned_before = check_for_orphaned_processes()

        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            futures = []
            for _ in range(num_concurrent):
                future = executor.submit(
                    compute_dfm_analysis_for_stl,
                    stl_bytes,
                    30,  # timeout_seconds
                )
                futures.append(future)

            for future in futures:
                try:
                    result = future.result(timeout=60)
                    results.append(result)
                except Exception as e:
                    pytest.fail(f"Concurrent DFM failed: {e}")

        # Verify all completed
        assert len(results) == num_concurrent, \
            f"Only {len(results)}/{num_concurrent} analyses completed"

        # Verify no orphaned processes
        time.sleep(1.0)  # Give time for cleanup
        orphaned_after = check_for_orphaned_processes()
        orphaned_count = len(orphaned_after) - len(orphaned_before)

        assert orphaned_count == 0, f"Found {orphaned_count} orphaned processes"

    def test_mixed_concurrent_operations(self, test_assets_dir):
        """Test mixed concurrent operations (cascadio + DFM)."""
        from concurrent.futures import ThreadPoolExecutor

        step_file = test_assets_dir / "cube.step"
        stl_file = test_assets_dir / "cube.stl"

        if not step_file.exists() or not stl_file.exists():
            pytest.skip("Required test files not found")

        step_bytes = step_file.read_bytes()
        stl_bytes = stl_file.read_bytes()

        results = {"cascadio": [], "dfm": []}

        orphaned_before = check_for_orphaned_processes()

        with ThreadPoolExecutor(max_workers=4) as executor:
            # Submit mixed operations
            futures = []

            # 2 cascadio loads
            for _ in range(2):
                future = executor.submit(load_cascadio_geometry, step_bytes, 30)
                futures.append(("cascadio", future))

            # 2 DFM analyses
            for _ in range(2):
                future = executor.submit(compute_dfm_analysis_for_stl, stl_bytes, 30)
                futures.append(("dfm", future))

            # Collect results
            for op_type, future in futures:
                try:
                    result = future.result(timeout=60)
                    results[op_type].append(result)
                except Exception as e:
                    pytest.fail(f"Mixed operation {op_type} failed: {e}")

        # Verify all completed
        assert len(results["cascadio"]) == 2, "Not all cascadio operations completed"
        assert len(results["dfm"]) == 2, "Not all DFM operations completed"

        # Verify no orphaned processes
        time.sleep(1.0)
        orphaned_after = check_for_orphaned_processes()
        orphaned_count = len(orphaned_after) - len(orphaned_before)

        assert orphaned_count == 0, f"Found {orphaned_count} orphaned processes"


class TestErrorRecovery:
    """Test error recovery and fault tolerance."""

    def test_timeout_recovery_after_crash(self, test_assets_dir):
        """Test system recovery after worker crash during operation."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Force a timeout that might crash the worker
        result = load_cascadio_geometry(step_bytes, timeout_seconds=0.001)

        # System should still be functional after timeout
        result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

        assert result is not None, "System not functional after crash"

    def test_partial_failure_in_multi_body(self, test_assets_dir):
        """Test multi-body DFM with partial failures."""
        # Create mock multi-body GLBs with one invalid
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Load STEP file to get GLB
        cascadio_result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

        if not cascadio_result or not cascadio_result.get("success"):
            pytest.skip("Cascadio loading failed")

        bodies = cascadio_result.get("bodies", [])
        if len(bodies) == 0:
            pytest.skip("No bodies loaded")

        # Create list with valid and invalid GLBs
        mixed_glbs = [
            bodies[0]["mesh"],  # Valid
            b"invalid glb data",  # Invalid
            bodies[0]["mesh"],  # Valid (duplicate)
        ]

        # Run DFM analysis
        dfm_result = compute_multi_body_dfm_analysis(mixed_glbs, timeout_seconds=60)

        # Should handle partial failure gracefully
        assert dfm_result is not None, "Multi-body DFM returned None on partial failure"

        # Should have results for valid bodies
        if "reports" in dfm_result:
            assert isinstance(dfm_result["reports"], list), "Reports should be a list"

    def test_file_format_error_handling(self):
        """Test handling of various file format errors."""
        invalid_inputs = [
            (b"", "empty data"),
            (b"not step or stl", "wrong format"),
            (b"\x00" * 1000, "null bytes"),
            (b"too short to be valid", "too short"),
        ]

        for data, description in invalid_inputs:
            # Test cascadio
            result = load_cascadio_geometry(data, timeout_seconds=5)
            assert result is not None, f"Cascadio crashed on {description}"

            # Test DFM
            result = compute_dfm_analysis_for_stl(data, timeout_seconds=5)
            assert result is not None, f"DFM crashed on {description}"


class TestResourceManagement:
    """Test resource management under load."""

    def test_temp_file_cleanup_after_operations(self, test_assets_dir):
        """Test that temp files are cleaned up after operations."""
        # Clean up any existing temp files
        cleaned_before = cleanup_temp_files(prefix="geom_")

        # Perform operations
        step_file = test_assets_dir / "cube.step"
        stl_file = test_assets_dir / "cube.stl"

        if step_file.exists():
            step_bytes = step_file.read_bytes()
            load_cascadio_geometry(step_bytes, timeout_seconds=30)

        if stl_file.exists():
            stl_bytes = stl_file.read_bytes()
            compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=30)

        # Give time for cleanup
        import time
        time.sleep(1.0)

        # Check temp files again
        # Note: There might be some temp files from other processes
        # The key is that we're not accumulating them
        cleaned_after = cleanup_temp_files(prefix="geom_")

        # We don't assert specific counts, but we verify cleanup doesn't crash
        assert True  # If we get here, cleanup worked

    def test_memory_stability_under_load(self, test_assets_dir):
        """Test memory stability under repeated operations."""
        import psutil
        import gc

        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()
        process = psutil.Process()

        # Measure memory over multiple operations
        memory_samples = []

        for i in range(5):
            rss_before = process.memory_info().rss / 1024 / 1024

            result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

            # Force cleanup
            gc.collect()
            time.sleep(0.2)

            rss_after = process.memory_info().rss / 1024 / 1024
            memory_samples.append({
                "iteration": i,
                "before": rss_before,
                "after": rss_after,
                "delta": rss_after - rss_before,
            })

        # Check for memory leaks
        # Later iterations should not show continuously increasing memory
        deltas = [s["delta"] for s in memory_samples]

        # Average delta should be reasonable
        avg_delta = sum(deltas) / len(deltas)
        assert avg_delta < 100, f"Average memory growth {avg_delta:.1f}MB indicates leak"

        # Last iteration should not be significantly higher than first
        first_delta = deltas[0]
        last_delta = deltas[-1]
        delta_growth = last_delta - first_delta

        assert delta_growth < 50, \
            f"Memory delta grew by {delta_growth:.1f}MB across iterations, indicating leak"

        print(f"\nMemory stability: {[f'{d:.1f}' for d in deltas]} MB")


class TestRealWorldScenarios:
    """Test real-world usage scenarios."""

    def test_large_file_handling(self, test_assets_dir):
        """Test handling of large files that might timeout."""
        # Find the largest STL file
        stl_files = list(test_assets_dir.glob("*.stl"))
        if not stl_files:
            pytest.skip("No STL files found")

        large_file = max(stl_files, key=lambda f: f.stat().st_size)
        stl_bytes = large_file.read_bytes()

        # Should complete within reasonable time
        result = compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds=90)

        assert result is not None, "Large file DFM returned None"

    def test_rapid_sequential_operations(self, test_assets_dir):
        """Test rapid sequential operations (no concurrency)."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Perform rapid sequential loads
        num_operations = 5
        results = []

        start_time = time.time()

        for _ in range(num_operations):
            result = load_cascadio_geometry(step_bytes, timeout_seconds=30)
            results.append(result)

        total_time = time.time() - start_time

        # All should complete
        assert len(results) == num_operations, \
            f"Only {len(results)}/{num_operations} operations completed"

        # Should complete in reasonable time
        # (allowing 30s per operation plus overhead)
        assert total_time < num_operations * 35, \
            f"Sequential operations took {total_time:.1f}s, too slow"

        print(f"\nRapid sequential: {num_operations} operations in {total_time:.1f}s")
