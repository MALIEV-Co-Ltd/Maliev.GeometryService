import io
from pathlib import Path

import pytest

from src.core.geometry import GeometryProcessor

ASSETS_DIR = Path(__file__).parent / "assets"


@pytest.fixture
def processor():
    return GeometryProcessor()


def test_analyze_cube(processor):
    cube_path = ASSETS_DIR / "cube.stl"
    with cube_path.open("rb") as f:
        stream = io.BytesIO(f.read())
        metrics = processor.analyze_stream(stream, ".stl")

    assert metrics.is_manifold is True
    assert pytest.approx(metrics.volume_cm3, abs=1e-3) == 1.0
    assert pytest.approx(metrics.surface_area_cm2, abs=1e-3) == 6.0
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
