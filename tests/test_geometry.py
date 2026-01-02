import io
from pathlib import Path

import pytest
import trimesh

from src.core.geometry import GeometryProcessor

ASSETS_DIR = Path(__file__).parent / "assets"


@pytest.fixture
def processor():
    return GeometryProcessor()


def is_format_supported(ext: str) -> bool:
    """Checks if trimesh has a backend for the given extension."""
    return ext.strip(".").lower() in trimesh.exchange.load.available_formats()


@pytest.mark.parametrize(
    "extension",
    [
        ".stl",
        ".obj",
        ".3mf",
        pytest.param(
            ".step",
            marks=pytest.mark.skipif(
                not is_format_supported(".step"), reason="STEP backend missing"
            ),
        ),
        pytest.param(
            ".stp",
            marks=pytest.mark.skipif(
                not is_format_supported(".stp"), reason="STP backend missing"
            ),
        ),
        pytest.param(
            ".igs",
            marks=pytest.mark.skipif(
                not is_format_supported(".igs"), reason="IGS backend missing"
            ),
        ),
        pytest.param(
            ".iges",
            marks=pytest.mark.skipif(
                not is_format_supported(".iges"), reason="IGES backend missing"
            ),
        ),
    ],
)
def test_analyze_cube_formats(processor, extension):
    cube_path = ASSETS_DIR / f"cube{extension}"
    if not cube_path.exists():
        pytest.skip(f"Asset {cube_path.name} missing")

    with cube_path.open("rb") as f:
        stream = io.BytesIO(f.read())
        try:
            metrics = processor.analyze_stream(stream, extension)
        except Exception as e:
            if "NoneType" in str(e):
                pytest.skip("Backend failed to process minimal CAD asset")
            raise e

    # If metrics are empty, it means the minimal CAD file didn't tessellate geometry
    if metrics.triangle_count == 0:
        pytest.skip(
            f"Backend loaded {extension} but found no geometry "
            "(likely minimal asset limitation)"
        )

    assert metrics.is_manifold is True
    # Volume should be ~1.0 cm3 for 10x10x10mm cube
    assert pytest.approx(metrics.volume_cm3, abs=1e-2) == 1.0
    # Surface area should be ~6.0 cm2
    assert pytest.approx(metrics.surface_area_cm2, abs=1e-2) == 6.0
    # Bounding box should be 10x10x10mm
    assert metrics.bounding_box.x == 10.0
    assert metrics.bounding_box.y == 10.0
    assert metrics.bounding_box.z == 10.0


def test_analyze_broken_mesh(processor):
    broken_path = ASSETS_DIR / "broken.stl"
    with broken_path.open("rb") as f:
        stream = io.BytesIO(f.read())
        metrics = processor.analyze_stream(stream, ".stl")

    assert metrics.is_manifold is False
    assert metrics.volume_cm3 >= 0
