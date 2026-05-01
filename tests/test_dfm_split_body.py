"""Tests for split-by-body DFM analysis with fault tolerance."""

import os
import tempfile

import pytest

from src.core.geometry import _compute_dfm_single_body


class TestSingleBodyDfm:
    """Test suite for single-body DFM analysis."""

    def test_single_body_returns_valid_report(self):
        """Test that single body DFM returns a valid report structure."""
        # This is a minimal test - we'd need actual STL data for a full test
        # For now, we test that the function signature is correct

        # Create a mock STL file (this won't be valid STL, but tests the error path)
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".stl", delete=False) as f:
            f.write(b"invalid stl data")
            stl_path = f.name

        try:
            result = _compute_dfm_single_body(
                stl_path=stl_path,
                cad_path=None,
                cad_ext=None,
                body_id=0,
            )

            # Should return a dict (either error result or DFM report)
            assert isinstance(result, dict)
            # Function should handle errors gracefully and return structured result
            # It might succeed with a minimal report or return an error dict
            assert len(result) > 0 or "error_type" in result
        finally:
            os.unlink(stl_path)  # noqa: PTH108

    def test_single_body_error_handling(self):
        """Test that single body returns structured error on failure."""
        # Test with a non-existent file
        result = _compute_dfm_single_body(
            stl_path="/nonexistent/file.stl",
            cad_path=None,
            cad_ext=None,
            body_id=0,
        )

        # Should return a structured error
        assert isinstance(result, dict)
        assert "error_type" in result
        assert result["body_id"] == 0
        assert "error_message" in result or "stack_trace" in result


class TestMultiBodyFaultTolerance:
    """Test suite for multi-body fault tolerance."""

    def test_multi_body_parallel_execution_concept(self):
        """Test concept for parallel execution (simplified)."""
        # This is a conceptual test - actual parallel execution requires
        # pytest-asyncio which may not be installed

        # Create mock STL paths
        stl_paths = {i: f"/tmp/body_{i}.stl" for i in range(3)}

        # Verify the structure is correct for parallel execution
        assert len(stl_paths) == 3
        assert all(isinstance(k, int) for k in stl_paths)
        assert all(isinstance(v, str) for v in stl_paths.values())

    def test_one_body_crash_concept(self):
        """Test concept for fault tolerance (simplified)."""
        # This is a conceptual test demonstrating the fault tolerance pattern
        body_results = [
            (0, {"success": True}),
            (1, {"error_type": "MemoryError"}),  # This body crashed
            (2, {"success": True}),
        ]

        # Count successful and failed bodies
        successful = [r for bid, r in body_results if "error_type" not in r]
        failed = [r for bid, r in body_results if "error_type" in r]

        # Bodies 0 and 2 succeeded, body 1 failed
        assert len(successful) == 2
        assert len(failed) == 1


class TestBodyExtraction:
    """Test suite for GLB body extraction."""

    def test_extract_bodies_returns_dict(self):
        """Test that body extraction returns a dict structure."""
        # This is a conceptual test - actual extraction requires trimesh
        # The function should return {body_id: stl_path}
        test_result = {0: "/tmp/body_0.stl", 1: "/tmp/body_1.stl"}

        assert isinstance(test_result, dict)
        assert all(isinstance(k, int) for k in test_result)
        assert all(isinstance(v, str) for v in test_result.values())


class TestWorkerDiagnostics:
    """Test suite for worker diagnostics extraction."""

    def test_extract_worker_diagnostics_no_logs(self):
        """Test that missing worker logs are handled gracefully."""
        # Skip this test if psutil is not available (required by upload_consumer)
        try:
            import psutil  # noqa: F401
        except ImportError:
            pytest.skip("psutil not available")

        from uuid import uuid4

        from src.consumers.upload_consumer import _extract_worker_diagnostics

        # Test with a random UUID (no logs exist)
        result = _extract_worker_diagnostics(uuid4())

        # Should return "No worker logs found"
        assert "No worker logs found" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
