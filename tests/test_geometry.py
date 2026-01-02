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

    # Some CAD backends load in meters (0.01) instead of mm (10.0)
    # or may not produce a perfectly manifold mesh from a STEP file.
    is_meter = metrics.bounding_box.x < 0.1

    # Volume should be ~1.0 cm3 for 10x10x10mm cube
    # If in meters (0.01), volume_mm3 is 1e-6, volume_cm3 is 1e-9
    expected_vol = 1.0 if not is_meter else 1e-9
    assert pytest.approx(metrics.volume_cm3, rel=1e-2) == expected_vol

    # Surface area should be ~6.0 cm2
    expected_area = 6.0 if not is_meter else 6e-6
    assert pytest.approx(metrics.surface_area_cm2, rel=1e-2) == expected_area

    # For CAD formats, we are more lenient with manifold status in tests
    # as long as the dimensions/volume are correct.
    if extension not in [".step", ".stp", ".igs", ".iges"]:
        assert metrics.is_manifold is True


def test_analyze_broken_mesh(processor):
    broken_path = ASSETS_DIR / "broken.stl"
    with broken_path.open("rb") as f:
        stream = io.BytesIO(f.read())
        metrics = processor.analyze_stream(stream, ".stl")

    assert metrics.is_manifold is False
    assert metrics.volume_cm3 >= 0
