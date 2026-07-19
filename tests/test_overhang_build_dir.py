"""Regression test for build-direction-aware overhang detection.

The legacy compute_overhang_analysis hard-coded +Z as the build direction,
so a part rotated 90° around X silently reported zero overhangs.  After
Stage 4 the function takes a ``build_dir`` parameter.  Rotating 90° around
the X axis in cadquery's right-hand convention sends the original part's
"up" axis (+Z) to -Y, so the equivalent build direction in the rotated
frame is -Y.  This test confirms:

* default +Z misses the rotated overhang;
* build_dir=(0, -1, 0) recovers it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

cadquery = pytest.importorskip("cadquery")  # noqa: F841

from src.core.geometry import _compute_metrics_worker  # noqa: E402
from src.core.mesh_analyzers import compute_overhang_analysis  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "dfm" / "overhang_45deg_rotated.step"


def _load_mesh():
    if not FIXTURE.exists():
        pytest.skip("fixture overhang_45deg_rotated.step not generated")
    metrics = _compute_metrics_worker(FIXTURE.read_bytes(), "step")
    stl_bytes = metrics.get("mesh_stl_bytes") or b""
    assert stl_bytes
    import io

    import trimesh

    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
    assert isinstance(mesh, trimesh.Trimesh)
    return mesh


def test_default_z_build_dir_misses_rotated_overhang() -> None:
    mesh = _load_mesh()
    count, _, _, _ = compute_overhang_analysis(mesh, threshold_deg=45.0)
    assert count == 0, (
        "Default +Z build direction should not see an overhang on a wedge "
        "whose downward face was rotated to point along +Y"
    )


def test_negative_y_build_dir_detects_rotated_overhang() -> None:
    mesh = _load_mesh()
    count, area_cm2, centroids, faces = compute_overhang_analysis(
        mesh,
        threshold_deg=45.0,
        build_dir=(0.0, -1.0, 0.0),
    )
    assert count >= 1, (
        f"build_dir=-Y should detect the rotated overhang; got "
        f"count={count}, area_cm2={area_cm2}, centroids={centroids}"
    )
    assert faces, "expected non-empty face_indices for the overhang region"
