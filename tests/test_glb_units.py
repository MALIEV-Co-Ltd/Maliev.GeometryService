import io

import pytest
import trimesh

from src.core.geometry import (
    GeometryProcessor,
    _analyze_bytes_worker,
    _compute_metrics_worker,
)


def _box_glb(extents: tuple[float, float, float]) -> bytes:
    mesh = trimesh.creation.box(extents=extents)
    return mesh.export(file_type="glb")


def test_compute_metrics_worker_keeps_millimeter_scale_glb_dimensions():
    glb = _box_glb((2614.690, 1871.386, 262.131))

    result = _compute_metrics_worker(glb, ".glb")

    assert result["bounding_box"]["x"] == pytest.approx(2614.690, rel=1e-5)
    assert result["bounding_box"]["y"] == pytest.approx(1871.386, rel=1e-5)
    assert result["bounding_box"]["z"] == pytest.approx(262.131, rel=1e-5)


def test_analyze_bytes_worker_keeps_millimeter_scale_glb_dimensions():
    glb = _box_glb((2614.690, 1871.386, 262.131))

    result = _analyze_bytes_worker(glb, ".glb")

    assert result["bounding_box"]["x"] == pytest.approx(2614.690, rel=1e-5)
    assert result["bounding_box"]["y"] == pytest.approx(1871.386, rel=1e-5)
    assert result["bounding_box"]["z"] == pytest.approx(262.131, rel=1e-5)


def test_geometry_processor_scales_subunit_glb_dimensions_to_millimeters():
    glb = _box_glb((0.12, 0.04, 0.025))
    processor = GeometryProcessor(enable_diagnostics=False)

    try:
        metrics, _, _ = processor.analyze_stream(io.BytesIO(glb), ".glb")
    finally:
        processor.shutdown()

    assert metrics.bounding_box.x == pytest.approx(120.0, rel=1e-5)
    assert metrics.bounding_box.y == pytest.approx(40.0, rel=1e-5)
    assert metrics.bounding_box.z == pytest.approx(25.0, rel=1e-5)
