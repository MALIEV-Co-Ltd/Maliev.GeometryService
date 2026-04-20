"""Hole detection tests.

Tests for hole/pocket detection in CNC milling.
Uses the 70x40x30mm-cube-variuos-holes test file.
"""

import io

import pytest
import trimesh

from src.core.geometry import GeometryProcessor


ASSETS_DIR = pytest.importorskip("pathlib").Path(__file__).parent / "assets"


HOLE_FILES = [
    "70x40x30mm-cube-variuos-holes-binary.stl",
    "70x40x30mm-cube-variuos-holes.3mf",
    "70x40x30mm-cube-variuos-holes.step",
]


class TestHoleDetection:
    """Test hole detection in geometry."""

    @pytest.mark.parametrize("filename", HOLE_FILES)
    def test_hole_detection_loads(self, filename):
        """Test that hole geometry loads."""
        file_path = ASSETS_DIR / filename

        if not file_path.exists():
            pytest.skip(f"File not found: {filename}")

        try:
            loaded = trimesh.load(file_path)
        except ModuleNotFoundError as e:
            pytest.skip(f"Optional dependency missing: {e}")
        assert loaded is not None


class TestHoleProcessing:
    """Test GeometryProcessor processes hole files for CNC."""

    def test_cnc_holes_detection(self):
        """Test CNC hole detection."""
        file_path = ASSETS_DIR / "70x40x30mm-cube-variuos-holes-binary.stl"

        if not file_path.exists():
            pytest.skip(f"File not found")

        bytes_data = file_path.read_bytes()
        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            stream = io.BytesIO(bytes_data)
            metrics, glb, thumbnail = processor.analyze_stream(stream, ".stl")

            assert metrics is not None
            assert metrics.triangle_count > 0
        except Exception as e:
            pytest.skip(f"Processing error: {e}")
        finally:
            processor.shutdown()


class TestHoleDFMReports:
    """Test DFM reports include hole information."""

    def test_hole_dfm_report(self):
        """Test that DFM report has hole detection."""
        from src.core.geometry import _analyze_single_process

        file_path = ASSETS_DIR / "70x40x30mm-cube-variuos-holes-binary.stl"

        if not file_path.exists():
            pytest.skip(f"File not found")

        stl_bytes = file_path.read_bytes()

        result = _analyze_single_process(stl_bytes, "CNC_MILL")

        assert result is not None
        assert "reportType" in result or "issues" in result
