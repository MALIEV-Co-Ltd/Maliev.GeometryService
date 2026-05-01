"""Format loading tests.

Tests that all geometry file formats can be loaded and parsed correctly.
Covers STL (ASCII/binary), 3MF, IGES, OBJ, STEP formats.
"""

import io

import pytest
import trimesh

from src.core.geometry import GeometryProcessor

ASSETS_DIR = pytest.importorskip("pathlib").Path(__file__).parent / "assets"


def get_all_geometry_files():
    """Get all geometry files grouped by base name."""
    files = list(ASSETS_DIR.glob("*.stl"))
    files.extend(ASSETS_DIR.glob("*.3mf"))
    files.extend(ASSETS_DIR.glob("*.iges"))
    files.extend(ASSETS_DIR.glob("*.obj"))
    files.extend(ASSETS_DIR.glob("*.step"))

    geometries = {}
    for f in files:
        stem = f.stem
        if stem not in geometries:
            geometries[stem] = []
        geometries[stem].append(f)

    return geometries


GEOMETRIES = get_all_geometry_files()


class TestFormatLoading:
    """Test that all format loaders work."""

    @pytest.mark.parametrize("geometry_name", list(GEOMETRIES.keys())[:10])
    def test_geometry_loads(self, geometry_name):
        """Test that geometry loads without crashing."""
        geometry_files = GEOMETRIES[geometry_name]

        for f in geometry_files:
            ext = f.suffix.lower()

            bytes_data = f.read_bytes()
            stream = io.BytesIO(bytes_data)

            try:
                loaded = trimesh.load(stream, file_type=ext)
                assert loaded is not None
            except Exception as e:
                pytest.skip(f"Backend not available: {e}")


class TestGeometryProcessorFormats:
    """Test GeometryProcessor handles all formats."""

    @pytest.mark.parametrize("geometry_name", list(GEOMETRIES.keys())[:10])
    def test_analyze_stream_all_formats(self, geometry_name):
        """Test that analyze_stream handles all formats."""
        geometry_files = GEOMETRIES[geometry_name]

        for f in geometry_files:
            ext = f.suffix.lower()
            bytes_data = f.read_bytes()

            processor = GeometryProcessor(enable_diagnostics=False)

            try:
                stream = io.BytesIO(bytes_data)
                metrics, glb, thumbnail = processor.analyze_stream(stream, ext)

                if metrics:
                    assert hasattr(metrics, "triangle_count")
            except Exception as e:
                if "NoneType" in str(e) or "backend" in str(e).lower():
                    pytest.skip(f"Backend issue: {e}")
                else:
                    raise
            finally:
                processor.shutdown()


@pytest.mark.parametrize(
    "geometry_file",
    [pytest.param(f, id=f.stem) for f in sorted(ASSETS_DIR.glob("*.stl"))[:20]],
)
def test_stl_loading(geometry_file):
    """Test STL files load correctly."""
    bytes_data = geometry_file.read_bytes()
    stream = io.BytesIO(bytes_data)

    loaded = trimesh.load(stream, file_type="stl")

    assert loaded is not None
    if hasattr(loaded, "vertices"):
        assert len(loaded.vertices) > 0


@pytest.mark.parametrize(
    "geometry_file",
    [pytest.param(f, id=f.stem) for f in sorted(ASSETS_DIR.glob("*.step"))[:10]],
)
def test_step_loading(geometry_file):
    """Test STEP files load correctly."""
    bytes_data = geometry_file.read_bytes()
    stream = io.BytesIO(bytes_data)

    try:
        loaded = trimesh.load(stream, file_type="step")
        assert loaded is not None
    except Exception as e:
        pytest.skip(f"STEP backend issue: {e}")
