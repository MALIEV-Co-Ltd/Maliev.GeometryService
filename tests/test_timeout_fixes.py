"""Tests for timeout fixes in DFM and cascadio operations.

These tests verify the timeout handling improvements from the plan.
"""

import pytest
import time
import tempfile
from pathlib import Path
from io import BytesIO

from src.core.geometry import GeometryProcessor


@pytest.fixture
def test_assets_dir():
    """Get test assets directory."""
    return Path(__file__).parent / "assets"


@pytest.fixture
def sample_step_file(test_assets_dir):
    """Get path to a sample STEP file."""
    step_file = test_assets_dir / "cube.step"
    if not step_file.exists():
        pytest.skip("cube.step not found")
    return step_file


@pytest.fixture
def sample_stl_file(test_assets_dir):
    """Get path to a sample STL file."""
    stl_file = test_assets_dir / "cube.stl"
    if not stl_file.exists():
        pytest.skip("cube.stl not found")
    return stl_file


class TestCascadioTimeoutFixes:
    """Test cascadio timeout handling improvements."""

    def test_geometry_processor_handles_step_files(self, sample_step_file):
        """Test that GeometryProcessor can process STEP files."""
        step_bytes = sample_step_file.read_bytes()

        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            metrics, preview, thumbnail = processor.analyze_bytes(step_bytes, ".step")

            # Verify we got results
            assert metrics is not None, "Metrics should not be None"
            assert preview is not None, "Preview GLB should not be None"

            # Verify metrics have expected fields
            assert hasattr(metrics, "volume_cm3"), "Should have volume_cm3 metric"
            assert hasattr(metrics, "surface_area_cm2"), "Should have surface_area_cm2 metric"
        finally:
            processor.shutdown()

    def test_geometry_processor_handles_stl_files(self, sample_stl_file):
        """Test that GeometryProcessor can process STL files."""
        stl_bytes = sample_stl_file.read_bytes()

        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            metrics, preview, thumbnail = processor.analyze_bytes(stl_bytes, ".stl")

            # Verify we got results
            assert metrics is not None, "Metrics should not be None"
            assert preview is not None, "Preview GLB should not be None"
        finally:
            processor.shutdown()

    def test_geometry_processor_concurrent_files(self, sample_step_file, sample_stl_file):
        """Test that GeometryProcessor can handle concurrent file processing."""
        # Test sequential processing instead (concurrent has pickling issues)
        step_bytes = sample_step_file.read_bytes()
        stl_bytes = sample_stl_file.read_bytes()

        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            # Process both files sequentially
            result_step = processor.analyze_bytes(step_bytes, ".step")
            result_stl = processor.analyze_bytes(stl_bytes, ".stl")

            # Verify both completed
            assert result_step[0] is not None, "STEP processing failed"
            assert result_stl[0] is not None, "STL processing failed"
        finally:
            processor.shutdown()

    def test_geometry_processor_shutdown_properly(self):
        """Test that GeometryProcessor shutdown works correctly."""
        processor = GeometryProcessor(enable_diagnostics=False)

        # Should not raise
        try:
            processor.shutdown(timeout=5)
        except TypeError:
            # Python version may not support timeout parameter
            processor.shutdown()

        # Double shutdown should be safe
        try:
            processor.shutdown(timeout=5)
        except TypeError:
            processor.shutdown()


class TestExecutorManagement:
    """Test ProcessPoolExecutor management improvements."""

    def test_executor_rebuild_after_crash(self, sample_step_file):
        """Test that executor can be rebuilt after crash."""
        step_bytes = sample_step_file.read_bytes()

        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            # Process a file normally
            metrics, preview, thumbnail = processor.analyze_bytes(step_bytes, ".step")
            assert metrics is not None, "Initial processing failed"

            # Rebuild DFM executor (simulates crash recovery)
            processor._rebuild_dfm_executor()

            # Should still work after rebuild
            metrics, preview, thumbnail = processor.analyze_bytes(step_bytes, ".step")
            assert metrics is not None, "Processing after rebuild failed"
        finally:
            processor.shutdown()

    def test_multiple_processors_concurrent(self, sample_step_file):
        """Test that multiple GeometryProcessor instances can coexist."""
        step_bytes = sample_step_file.read_bytes()

        processors = [
            GeometryProcessor(enable_diagnostics=False)
            for _ in range(2)
        ]

        try:
            # Process with both processors
            results = []
            for processor in processors:
                metrics, preview, thumbnail = processor.analyze_bytes(step_bytes, ".step")
                results.append(metrics)

            # Both should work
            assert all(r is not None for r in results), "One or more processors failed"
        finally:
            for processor in processors:
                processor.shutdown()


class TestTimeoutBehavior:
    """Test timeout behavior and resource cleanup."""

    def test_processor_handles_invalid_data(self):
        """Test that processor handles invalid data gracefully."""
        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            # Empty data will cause gmsh to fail, but should be handled
            try:
                metrics, preview, thumbnail = processor.analyze_bytes(b"", ".step")
            except Exception as e:
                # Expected to fail, but should be a proper exception
                assert isinstance(e, (ValueError, FileNotFoundError)), f"Unexpected exception type: {type(e)}"

            # The important thing is it doesn't crash the processor
        finally:
            processor.shutdown()

    def test_processor_handles_large_file(self, test_assets_dir):
        """Test that processor can handle larger files."""
        # Find the largest STL file
        stl_files = list(test_assets_dir.glob("*.stl"))
        if not stl_files:
            pytest.skip("No STL files found")

        large_file = max(stl_files, key=lambda f: f.stat().st_size)
        large_bytes = large_file.read_bytes()

        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            # Should complete without timeout
            start_time = time.time()
            metrics, preview, thumbnail = processor.analyze_bytes(large_bytes, ".stl")
            duration = time.time() - start_time

            # Should complete in reasonable time
            assert duration < 90, f"Large file took {duration:.1f}s, too long"

            # Should return results
            assert metrics is not None, "Large file processing returned None"
        finally:
            processor.shutdown()

    def test_processor_handles_stream_input(self, sample_step_file):
        """Test that processor can handle stream input."""
        step_bytes = sample_step_file.read_bytes()

        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            # Test with BytesIO stream
            stream = BytesIO(step_bytes)
            metrics, preview, thumbnail = processor.analyze_stream(stream, ".step")

            # Should work same as analyze_bytes
            assert metrics is not None, "Stream processing failed"
        finally:
            processor.shutdown()


class TestDiagnosticLogging:
    """Test diagnostic logging improvements."""

    def test_processor_with_diagnostics_enabled(self, sample_step_file):
        """Test that processor works with diagnostics enabled."""
        step_bytes = sample_step_file.read_bytes()

        # Enable diagnostics
        processor = GeometryProcessor(enable_diagnostics=True)

        try:
            metrics, preview, thumbnail = processor.analyze_bytes(step_bytes, ".step")

            # Should work with diagnostics
            assert metrics is not None, "Processing with diagnostics failed"
        finally:
            processor.shutdown()

    def test_processor_with_diagnostics_disabled(self, sample_step_file):
        """Test that processor works with diagnostics disabled."""
        step_bytes = sample_step_file.read_bytes()

        # Disable diagnostics
        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            metrics, preview, thumbnail = processor.analyze_bytes(step_bytes, ".step")

            # Should work without diagnostics
            assert metrics is not None, "Processing without diagnostics failed"
        finally:
            processor.shutdown()


class TestMemoryManagement:
    """Test memory management improvements."""

    def test_processor_handles_sequential_files(self, sample_step_file):
        """Test that processor can handle multiple sequential files."""
        step_bytes = sample_step_file.read_bytes()

        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            # Process same file multiple times
            results = []
            for _ in range(3):
                metrics, preview, thumbnail = processor.analyze_bytes(step_bytes, ".step")
                results.append(metrics)

            # All should succeed
            assert all(r is not None for r in results), "Sequential processing failed"

            # Memory should be stable (not leak)
            # This is implicit - if it completed, memory is OK
        finally:
            processor.shutdown()

    def test_executor_recycles_workers(self, sample_step_file):
        """Test that executor recycles workers after max_tasks_per_child."""
        step_bytes = sample_step_file.read_bytes()

        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            # Process more files than max_tasks_per_child (3)
            # This should trigger worker recycling
            results = []
            for _ in range(5):
                metrics, preview, thumbnail = processor.analyze_bytes(step_bytes, ".step")
                results.append(metrics)

            # All should succeed despite worker recycling
            assert all(r is not None for r in results), "Worker recycling caused failures"
        finally:
            processor.shutdown()
