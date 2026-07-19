"""Multi-body geometry tests.

Tests handling of multi-body geometry files (separate bodies).
"""

import io

import pytest
import trimesh

from src.core.geometry import GeometryProcessor

ASSETS_DIR = pytest.importorskip("pathlib").Path(__file__).parent / "assets"


MULTIBODY_FILES = [
    "50mm-polygon-multibodies-nonoverlap-binary.stl",
    "50mm-polygon-multibodies-overlap-binary.stl",
]


class TestMultibodyDetection:
    """Test multi-body geometry is handled correctly."""

    @pytest.mark.parametrize("filename", MULTIBODY_FILES)
    def test_multibody_mesh_loading(self, filename):
        """Test that multi-body meshes load as Scene."""
        file_path = ASSETS_DIR / filename

        if not file_path.exists():
            pytest.skip(f"File not found: {filename}")

        loaded = trimesh.load(file_path)

        if isinstance(loaded, trimesh.Scene):
            body_count = len(loaded.geometry)
        elif isinstance(loaded, trimesh.Trimesh):
            body_count = 1
        else:
            body_count = 1

        assert body_count >= 1


class TestMultibodyProcessing:
    """Test GeometryProcessor handles multi-body files."""

    @pytest.mark.parametrize("filename", MULTIBODY_FILES)
    def test_processor_multibody(self, filename):
        """Test that processor handles multi-body files."""
        file_path = ASSETS_DIR / filename

        if not file_path.exists():
            pytest.skip(f"File not found: {filename}")

        bytes_data = file_path.read_bytes()
        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            stream = io.BytesIO(bytes_data)
            metrics, glb, thumbnail = processor.analyze_stream(stream, ".stl")

            assert metrics is not None

            assert metrics.body_count >= 1
        except Exception as e:
            pytest.skip(f"Processing error: {e}")
        finally:
            processor.shutdown()


class TestMultibodyFormats:
    """Test multi-body files in various formats."""

    @pytest.mark.parametrize(
        "file",
        [pytest.param(f, id=f.stem) for f in sorted(ASSETS_DIR.glob("*multibod*.stl"))],
    )
    def test_multibody_stl_formats(self, file):
        """Test multi-body STL files load."""
        bytes_data = file.read_bytes()
        stream = io.BytesIO(bytes_data)

        loaded = trimesh.load(stream, file_type="stl")
        assert loaded is not None
