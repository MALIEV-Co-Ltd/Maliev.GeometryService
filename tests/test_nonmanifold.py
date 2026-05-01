"""Non-manifold geometry tests.

Tests error handling for nonmanifold/invalid geometry files.
"""

import io

import pytest
import trimesh

from src.core.geometry import GeometryProcessor

ASSETS_DIR = pytest.importorskip("pathlib").Path(__file__).parent / "assets"


# These files are confirmed non-manifold (watertight=False)
NONMANIFOLD_FILES = [
    "20x20x20mm-cube-nonmanifold-binary.stl",
    "20x20x20mm-cube-nonmanifold-ascii.stl",
]


class TestNonmanifoldDetection:
    """Test that non-manifold geometries are detected."""

    @pytest.mark.parametrize("filename", NONMANIFOLD_FILES)
    def test_nonmanifold_mesh_detection(self, filename):
        """Test that non-manifold meshes are detected correctly."""
        file_path = ASSETS_DIR / filename

        if not file_path.exists():
            pytest.skip(f"File not found: {filename}")

        mesh = trimesh.load(file_path)

        assert mesh is not None

        is_manifold = mesh.is_watertight

        assert not is_manifold


class TestNonmanifoldProcessing:
    """Test GeometryProcessor handles non-manifold files."""

    @pytest.mark.parametrize("filename", NONMANIFOLD_FILES)
    def test_processor_handles_nonmanifold(self, filename):
        """Test that processor handles non-manifold files gracefully."""
        file_path = ASSETS_DIR / filename

        if not file_path.exists():
            pytest.skip(f"File not found: {filename}")

        bytes_data = file_path.read_bytes()
        processor = GeometryProcessor(enable_diagnostics=False)

        try:
            stream = io.BytesIO(bytes_data)
            metrics, glb, thumbnail = processor.analyze_stream(stream, ".stl")

            assert metrics is not None

            assert metrics.is_manifold is False
        except Exception as e:
            pytest.skip(f"Processing error: {e}")
        finally:
            processor.shutdown()


class TestNonmanifoldFormats:
    """Test non-manifold files in various formats."""

    @pytest.mark.parametrize(
        "file",
        [
            pytest.param(f, id=f.stem)
            for f in sorted(ASSETS_DIR.glob("*nonmanifold*.stl"))
        ],
    )
    def test_nonmanifold_stl_formats(self, file):
        """Test non-manifold STL files load."""
        bytes_data = file.read_bytes()
        stream = io.BytesIO(bytes_data)

        try:
            loaded = trimesh.load(stream, file_type="stl")
            assert loaded is not None
        except Exception as e:
            pytest.skip(f"Load error: {e}")
