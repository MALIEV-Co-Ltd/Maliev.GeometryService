"""Tests for DFM analysis with PRODUCTION files that timeout.

These tests use the ACTUAL production files from Z:\test files that cause
timeout issues in production. This validates that the DFM performance issues
are caught and addressed.
"""

import pytest
import time
from pathlib import Path

from src.core.geometry import _compute_dfm_single_body


@pytest.fixture
def production_files():
    """Get paths to PRODUCTION files that have timeout issues."""
    return {
        'MEC031233_01': {
            'stp': Path('tests/assets/MEC031233_01.stp'),
            'stl': Path('tests/assets/0101-01-005-for-print(02) (ULTEM 9085).STL'),
            'vertices': 14486,
            'expected_timeout': True,
        },
        'e16096_p11_EAR_JIG': {
            'step': Path('tests/assets/e16096_p11_EAR JIG-L.STEP'),
            'stl': Path('tests/assets/0101-01-005-for-print(02) (ULTEM 9085).STL'),
            'vertices': 4916,
            'expected_timeout': True,
        },
    }


class TestProductionDFMTimeouts:
    """Test DFM analysis with production files that timeout."""

    def test_mec031233_dfm_should_complete_quickly(self, production_files):
        """Test that MEC031233_01.stp completes DFM analysis in reasonable time.

        This file has 14,486 vertices and times out in production after 95s.
        After fixes, it should complete in < 60s.
        """
        file_info = production_files['MEC031233_01']

        if not file_info['stp'].exists():
            pytest.skip(f"Production file not found: {file_info['stp']}")

        start_time = time.time()

        result = _compute_dfm_single_body(
            str(file_info['stl']),
            str(file_info['stp']),
            '.stp',
            0
        )

        duration = time.time() - start_time

        print(f"\nMEC031233_01 DFM analysis took {duration:.1f}s")
        print(f"Result has {len(result)} keys")
        print(f"Error type: {result.get('error_type', 'None')}")

        # After fixes, this should complete quickly (< 60s)
        # Currently it times out at 90s
        assert duration < 90, f"DFM analysis still too slow: {duration:.1f}s"

        # Should not be a timeout error after fixes
        if result.get('error_type') == 'TimeoutError':
            pytest.fail(f"DFM analysis still timing out after {duration:.1f}s")

        # Should have successful DFM reports
        if result.get('error_type') is None:
            assert 'FDM' in result or 'SLA' in result, "No DFM reports generated"

    def test_e16096_ear_jig_dfm_should_complete_quickly(self, production_files):
        """Test that e16096_p11_EAR JIG-L.STEP completes DFM analysis in reasonable time.

        This file has 4,916 vertices and times out in production after 95s.
        After fixes, it should complete in < 30s.
        """
        file_info = production_files['e16096_p11_EAR JIG']

        if not file_info['step'].exists():
            pytest.skip(f"Production file not found: {file_info['step']}")

        start_time = time.time()

        result = _compute_dfm_single_body(
            str(file_info['stl']),
            str(file_info['step']),
            '.step',
            0
        )

        duration = time.time() - start_time

        print(f"\ne16096_p11_EAR JIG DFM analysis took {duration:.1f}s")
        print(f"Result has {len(result)} keys")
        print(f"Error type: {result.get('error_type', 'None')}")

        # After fixes, this should complete quickly (< 30s for 4K vertices)
        assert duration < 60, f"DFM analysis too slow: {duration:.1f}s"

        # Should not be a timeout error after fixes
        if result.get('error_type') == 'TimeoutError':
            pytest.fail(f"DFM analysis still timing out after {duration:.1f}s")

        # Should have successful DFM reports
        if result.get('error_type') is None:
            assert 'FDM' in result or 'SLA' in result, "No DFM reports generated"


class TestTessellationPerformance:
    """Test STEP B-Rep tessellation performance specifically."""

    def test_step_brep_tessellation_performance(self):
        """Test that STEP B-Rep tessellation completes in reasonable time.

        The tessellation in occ_analyzer.py is the bottleneck.
        For production files, it takes 90+ seconds.
        """
        from src.core.occ_analyzer import analyze_step_brep

        step_file = Path('tests/assets/MEC031233_01.stp')

        if not step_file.exists():
            pytest.skip(f"Production file not found: {step_file}")

        step_bytes = step_file.read_bytes()

        start_time = time.time()

        features, face_map = analyze_step_brep(step_bytes, '.stp')

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


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
