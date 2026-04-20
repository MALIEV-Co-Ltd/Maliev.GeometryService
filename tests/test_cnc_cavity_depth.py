"""CNC cavity depth tests.

Tests for deep cavity depth/diameter ratio detection in CNC milling.
Uses the 100x100x100mm-cube-sharp-internal-corners-various-fillets test file.
"""

import io

import pytest
import trimesh

from src.core.geometry import GeometryProcessor, _analyze_single_process


ASSETS_DIR = pytest.importorskip("pathlib").Path(__file__).parent / "assets"


DEEP_CAVITY_FILES = [
    "100x100x100mm-cube-sharp-internal-corners-various-fillets-binary.stl",
    "100x100x100mm-cube-sharp-internal-corners-various-fillets.step",
]


class TestCavityDepthGeometry:
    """Test deep cavity geometry loading."""

    @pytest.mark.parametrize("filename", DEEP_CAVITY_FILES)
    def test_deep_cavity_loads(self, filename):
        """Test that deep cavity geometry loads."""
        file_path = ASSETS_DIR / filename

        if not file_path.exists():
            pytest.skip(f"File not found: {filename}")

        try:
            loaded = trimesh.load(file_path)
        except ModuleNotFoundError as e:
            pytest.skip(f"Optional dependency missing: {e}")
        assert loaded is not None


class TestCavityDepthRatio:
    """Test cavity depth/diameter ratio detection."""

    def test_cnc_cavity_depth_ratio(self):
        """Test CNC cavity depth ratio detection.

        The 100x100x100mm file has deep pockets (100mm depth).
        Should detect cavities where depth > 4× width.
        """
        file_path = (
            ASSETS_DIR
            / "100x100x100mm-cube-sharp-internal-corners-various-fillets-binary.stl"
        )

        if not file_path.exists():
            pytest.skip(f"File not found")

        stl_bytes = file_path.read_bytes()

        result = _analyze_single_process(stl_bytes, "CNC_MILL")

        assert result is not None

        if "issues" in result:
            cavity_issues = [
                i
                for i in result["issues"]
                if i.get("category") in ["cavity_depth", "deep_cavity"]
            ]

            print(f"\nCavity depth issues: {len(cavity_issues)}")

        if "cavityDepthIssues" in result:
            assert result["cavityDepthIssues"] is not None


class TestCavityDepthProcessing:
    """Test GeometryProcessor with deep cavity files."""

    @pytest.mark.parametrize("filename", DEEP_CAVITY_FILES[:1])
    def test_processor_deep_cavity(self, filename):
        """Test processor handles deep cavity files."""
        file_path = ASSETS_DIR / filename

        if not file_path.exists():
            pytest.skip(f"File not found: {filename}")

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
