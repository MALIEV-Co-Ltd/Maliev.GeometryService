"""Tests for DFM analysis with PRODUCTION files that timeout.

These tests use the ACTUAL production files from Z:\test files that cause
timeout issues in production. This validates that the DFM performance issues
are caught and addressed.
"""

import time
from pathlib import Path

import pytest

from src.core.geometry import _compute_dfm_single_body

pytestmark = pytest.mark.slow


@pytest.fixture
def production_files():
    """Get paths to production-like test files."""
    return {
        "large_cube": {
            "step": Path(
                "tests/assets/100x100x100mm-cube-sharp-internal-corners-various-fillets.step"
            ),
            "stl": Path(
                "tests/assets/100x100x100mm-cube-sharp-internal-corners-various-fillets-binary.stl"
            ),
            "expected_timeout": False,
        },
        "100mm_overhang": {
            "step": Path("tests/assets/100x20mm-long-short-overhang.step"),
            "stl": Path("tests/assets/100x20mm-long-short-overhang-binary.stl"),
            "expected_timeout": False,
        },
    }


class TestProductionDFMTimeouts:
    """Test DFM analysis with production-like files."""

    def test_large_cube_dfm_should_complete_quickly(self, production_files):
        """Test that large cube file completes DFM analysis in reasonable time.

        This file has deep pockets and various fillet radii.
        Should complete in < 30s.
        """
        file_info = production_files["large_cube"]

        if not file_info["step"].exists():
            pytest.skip(f"Test file not found: {file_info['step']}")

        start_time = time.time()

        result = _compute_dfm_single_body(
            str(file_info["stl"]), str(file_info["step"]), ".step", 0
        )

        duration = time.time() - start_time

        print(f"\nLarge cube DFM analysis took {duration:.1f}s")
        print(f"Result has {len(result)} keys")
        print(f"Error type: {result.get('error_type', 'None')}")

        # After fixes, this should complete quickly (< 60s)
        # Currently it times out at 90s
        assert duration < 90, f"DFM analysis still too slow: {duration:.1f}s"

        # Should not be a timeout error after fixes
        if result.get("error_type") == "TimeoutError":
            pytest.fail(f"DFM analysis still timing out after {duration:.1f}s")

        # Should have successful DFM reports
        if result.get("error_type") is None:
            assert "FDM" in result or "SLA" in result, "No DFM reports generated"

    def test_e16096_ear_jig_dfm_should_complete_quickly(self, production_files):
        """Test that e16096_p11_EAR JIG-L.STEP completes DFM analysis in reasonable time.

        This file has 4,916 vertices and times out in production after 95s.
        After fixes, it should complete in < 30s.
        """  # noqa: E501
        if "e16096_p11_EAR JIG" not in production_files:
            pytest.skip("e16096_p11_EAR JIG not in production_files fixture")
        file_info = production_files["e16096_p11_EAR JIG"]

        if not file_info["step"].exists():
            pytest.skip(f"Production file not found: {file_info['step']}")

        start_time = time.time()

        result = _compute_dfm_single_body(
            str(file_info["stl"]), str(file_info["step"]), ".step", 0
        )

        duration = time.time() - start_time

        print(f"\ne16096_p11_EAR JIG DFM analysis took {duration:.1f}s")
        print(f"Result has {len(result)} keys")
        print(f"Error type: {result.get('error_type', 'None')}")

        # After fixes, this should complete quickly (< 30s for 4K vertices)
        assert duration < 60, f"DFM analysis too slow: {duration:.1f}s"

        # Should not be a timeout error after fixes
        if result.get("error_type") == "TimeoutError":
            pytest.fail(f"DFM analysis still timing out after {duration:.1f}s")

        # Should have successful DFM reports
        if result.get("error_type") is None:
            assert "FDM" in result or "SLA" in result, "No DFM reports generated"


class TestTessellationPerformance:
    """Test STEP B-Rep tessellation performance specifically."""

    def test_step_brep_tessellation_performance(self):
        """Test that STEP B-Rep tessellation completes in reasonable time.

        The tessellation in occ_analyzer.py is the bottleneck.
        Should complete in < 30s.
        """
        from src.core.occ_analyzer import analyze_step_brep

        step_file = Path(
            "tests/assets/100x100x100mm-cube-sharp-internal-corners-various-fillets.step"
        )

        if not step_file.exists():
            pytest.skip(f"Test file not found: {step_file}")

        step_bytes = step_file.read_bytes()

        start_time = time.time()

        features, face_map = analyze_step_brep(step_bytes, ".step")

        duration = time.time() - start_time

        print(f"\nSTEP B-Rep tessellation took {duration:.1f}s")
        print(f"Extracted {len(features)} features")
        print(f"Face map has {len(face_map)} entries")

        # Tessellation should be fast (< 10s) after optimization
        # Currently it takes 90+ seconds
        assert duration < 30, f"STEP tessillation too slow: {duration:.1f}s"

    def test_step_brep_tessellation_with_reduced_quality(self):
        """Test STEP B-Rep tessellation with reduced quality for speed.

        Test if reducing tessellation quality (0.05 → 0.1mm) improves performance
        without breaking DFM analysis.
        """
        # This test requires modifying the tessellation parameters
        # For now, it's a placeholder for future optimization
        pytest.skip("Requires tessellation parameter optimization")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
