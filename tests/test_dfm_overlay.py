"""
Tests for DFM overlay generation and the new features introduced in the
STEP-overlay-hotfix + multi-body + small-feature accuracy work.

Covers:
  - Issue 1: overlay GLBs are in mm (no 0.001 scale for STEP)
  - Issue 2: multi-body detection and per-body coloured overlay GLB
  - Issue 3: detect_small_features_occ avoids false positives on curved surfaces;
             triangle-edge fallback curvature/size filters

All tests use synthetic trimesh geometry — no STEP file loading required.
"""
from __future__ import annotations

import io
import math

import numpy as np
import pytest
import trimesh

# ---------------------------------------------------------------------------
# Issue 1 — overlay unit correctness
# ---------------------------------------------------------------------------


def _make_cube_mesh(side_mm: float = 10.0) -> trimesh.Trimesh:
    """Return a simple box in Z-up, mm."""
    box = trimesh.creation.box(extents=[side_mm, side_mm, side_mm])
    return box


def _overlay_bounding_box_diagonal(glb_bytes: bytes) -> float:
    """Load GLB and return bounding-box diagonal in the GLB coordinate units."""
    loaded = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")
    if isinstance(loaded, trimesh.Scene):
        mesh = loaded.to_geometry()
    else:
        mesh = loaded
    extents = mesh.extents
    return float(np.linalg.norm(extents))


class TestOverlayUnits:
    """generate_overlay_glb must produce a GLB in mm (no 0.001 scale-down)."""

    def test_overlay_glb_diagonal_matches_source_mesh_mm(self):
        from src.core.overlay_generator import generate_overlay_glb

        side = 20.0  # mm
        mesh = _make_cube_mesh(side)
        # Use all faces
        face_indices = list(range(len(mesh.faces)))

        glb = generate_overlay_glb(mesh, face_indices, "thin_wall",
                                   reference_center=mesh.center_mass)
        assert glb is not None, "generate_overlay_glb returned None"

        diag = _overlay_bounding_box_diagonal(glb)
        # The GLB is centered (translation applied) and Z→Y rotated.
        # Its bounding box diagonal should be approximately sqrt(3)*side for a cube.
        expected = math.sqrt(3) * side
        # Allow ±10% for rotation / centering rounding.
        assert abs(diag - expected) / expected < 0.10, (
            f"GLB diagonal {diag:.2f} mm is far from expected {expected:.2f} mm — "
            "overlay may have been scaled by 0.001 (STEP unit bug)"
        )

    def test_overlay_glb_not_in_meters(self):
        """A 20 mm cube overlay must not produce a ~0.02 m bounding box."""
        from src.core.overlay_generator import generate_overlay_glb

        mesh = _make_cube_mesh(20.0)
        glb = generate_overlay_glb(mesh, list(range(len(mesh.faces))), "overhang")
        assert glb is not None

        diag = _overlay_bounding_box_diagonal(glb)
        # If the 0.001 bug were present the diagonal would be ~0.035 (meters).
        # A correct mm overlay has diagonal ~34.6.
        assert diag > 1.0, (
            f"Overlay GLB diagonal is {diag:.4f} — looks like meters, expected mm"
        )

    def test_generate_overlay_glb_no_output_in_meters_param(self):
        """The output_in_meters parameter must no longer exist (was removed)."""
        import inspect
        from src.core.overlay_generator import generate_overlay_glb

        sig = inspect.signature(generate_overlay_glb)
        assert "output_in_meters" not in sig.parameters, (
            "output_in_meters parameter was expected to be removed"
        )


# ---------------------------------------------------------------------------
# Issue 2 — multi-body overlay
# ---------------------------------------------------------------------------


def _make_two_body_mesh() -> list[trimesh.Trimesh]:
    """Two spatially separated boxes — simulates a multi-body STEP assembly."""
    body1 = trimesh.creation.box([5, 5, 5])
    body1.apply_translation([0, 0, 0])
    body2 = trimesh.creation.box([5, 5, 5])
    body2.apply_translation([20, 0, 0])  # far apart so split() recovers them
    return [body1, body2]


class TestMultiBodyOverlay:
    """generate_multi_body_overlay_glb produces a correct per-body coloured GLB."""

    def test_returns_bytes_for_two_bodies(self):
        from src.core.overlay_generator import generate_multi_body_overlay_glb

        bodies = _make_two_body_mesh()
        center = trimesh.util.concatenate(bodies).center_mass

        glb = generate_multi_body_overlay_glb(bodies, center)
        assert glb is not None
        assert isinstance(glb, bytes)
        assert len(glb) > 0

    def test_glb_contains_vertex_colors(self):
        """Each body must be coloured — the GLB must carry per-face colour data."""
        from src.core.overlay_generator import generate_multi_body_overlay_glb

        bodies = _make_two_body_mesh()
        center = trimesh.util.concatenate(bodies).center_mass
        glb = generate_multi_body_overlay_glb(bodies, center)
        assert glb is not None

        loaded = trimesh.load(io.BytesIO(glb), file_type="glb")
        if isinstance(loaded, trimesh.Scene):
            mesh = loaded.to_geometry()
        else:
            mesh = loaded

        # trimesh stores vertex colours in visual.vertex_colors (RGBA Nx4).
        # After concatenate() from trimesh they should be present.
        vc = mesh.visual.vertex_colors if hasattr(mesh.visual, "vertex_colors") else None
        assert vc is not None and len(vc) > 0, "Multi-body overlay GLB has no vertex colours"

    def test_two_bodies_have_different_colors(self):
        """Body 0 (red) and body 1 (blue) must produce visually distinct colours."""
        from src.core.overlay_generator import generate_multi_body_overlay_glb

        bodies = _make_two_body_mesh()
        center = trimesh.util.concatenate(bodies).center_mass
        glb = generate_multi_body_overlay_glb(bodies, center)
        assert glb is not None

        loaded = trimesh.load(io.BytesIO(glb), file_type="glb")
        if isinstance(loaded, trimesh.Scene):
            mesh = loaded.to_geometry()
        else:
            mesh = loaded

        vc = mesh.visual.vertex_colors  # (N, 4) uint8 RGBA
        assert vc is not None and len(vc) >= 2

        # The first body is red (R>>G,B) and the second is blue (B>>R,G).
        # After concatenation the first half of vertices is body0, second is body1.
        half = len(vc) // 2
        first_body_r = float(vc[:half, 0].mean())
        first_body_b = float(vc[:half, 2].mean())
        second_body_r = float(vc[half:, 0].mean())
        second_body_b = float(vc[half:, 2].mean())

        assert first_body_r > first_body_b, "First body should be red-dominant"
        assert second_body_b > second_body_r, "Second body should be blue-dominant"

    def test_returns_none_for_empty_list(self):
        from src.core.overlay_generator import generate_multi_body_overlay_glb

        result = generate_multi_body_overlay_glb([], np.zeros(3))
        assert result is None

    def test_overlay_in_mm_not_meters(self):
        """Multi-body overlay must stay in mm (not scaled to meters)."""
        from src.core.overlay_generator import generate_multi_body_overlay_glb

        bodies = _make_two_body_mesh()  # 5mm boxes, 20mm apart → span ~25mm
        center = trimesh.util.concatenate(bodies).center_mass
        glb = generate_multi_body_overlay_glb(bodies, center)
        assert glb is not None

        diag = _overlay_bounding_box_diagonal(glb)
        # If output were in meters, diag would be ~0.025. In mm it should be ~25.
        assert diag > 1.0, f"Multi-body overlay diagonal {diag:.4f} looks like meters"


def test_generate_overlays_worker_produces_multi_body_key():
    """_generate_overlays_worker generates GENERAL__multi_body key for multi-body STL."""
    from src.core.geometry import _generate_overlays_worker

    # Build two separated boxes and export as STL.
    body1 = trimesh.creation.box([5, 5, 5])
    body2 = trimesh.creation.box([5, 5, 5])
    body2.apply_translation([30, 0, 0])
    combined = trimesh.util.concatenate([body1, body2])
    stl_bytes = combined.export(file_type="stl")

    result = _generate_overlays_worker(stl_bytes, reports={})
    assert "GENERAL__multi_body" in result, (
        "Expected GENERAL__multi_body key for a two-body mesh"
    )


def test_generate_overlays_worker_no_multi_body_key_for_single_body():
    """_generate_overlays_worker must NOT produce GENERAL__multi_body for a single body."""
    from src.core.geometry import _generate_overlays_worker

    mesh = trimesh.creation.box([10, 10, 10])
    stl_bytes = mesh.export(file_type="stl")

    result = _generate_overlays_worker(stl_bytes, reports={})
    assert "GENERAL__multi_body" not in result, (
        "Single-body mesh should not produce a multi_body overlay"
    )


# ---------------------------------------------------------------------------
# Issue 3 — small feature detection accuracy
# ---------------------------------------------------------------------------


class TestDetectSmallFeaturesOcc:
    """detect_small_features_occ should use CAD topology, not triangle size."""

    @staticmethod
    def _make_occ_feature(
        feature_type: str,
        params: dict,
        centroid: list[float],
        area_mm2: float = 1.0,
    ):
        """Build a minimal duck-type OccFeature for testing."""
        from types import SimpleNamespace
        return SimpleNamespace(
            feature_type=feature_type,
            parameters=params,
            centroid=centroid,
            area_mm2=area_mm2,
        )

    def test_cylinder_below_threshold_is_flagged(self):
        from src.core.mesh_analyzers import detect_small_features_occ

        feat = self._make_occ_feature(
            "hole", {"radius_mm": 0.2}, [0.0, 0.0, 0.0]
        )
        count, centroids, _ = detect_small_features_occ([feat], min_size_mm=1.0)
        assert count == 1
        assert len(centroids) == 1

    def test_cylinder_above_threshold_not_flagged(self):
        from src.core.mesh_analyzers import detect_small_features_occ

        feat = self._make_occ_feature(
            "hole", {"radius_mm": 2.0}, [0.0, 0.0, 0.0]
        )
        count, _, _ = detect_small_features_occ([feat], min_size_mm=1.0)
        assert count == 0

    def test_torus_fillet_below_threshold_flagged(self):
        from src.core.mesh_analyzers import detect_small_features_occ

        # minor radius = 0.3 mm → feature_size = 0.6 mm < 1.0 mm threshold
        feat = self._make_occ_feature(
            "fillet", {"radius_mm": 0.3, "is_torus": True}, [5.0, 5.0, 5.0]
        )
        count, _, _ = detect_small_features_occ([feat], min_size_mm=1.0)
        assert count == 1

    def test_large_planar_face_not_flagged(self):
        from src.core.mesh_analyzers import detect_small_features_occ

        # sqrt(100) = 10 mm >> 1.0 mm threshold
        feat = self._make_occ_feature(
            "planar_face", {"area_mm2": 100.0}, [0.0, 0.0, 0.0], area_mm2=100.0
        )
        count, _, _ = detect_small_features_occ([feat], min_size_mm=1.0)
        assert count == 0

    def test_small_planar_face_flagged(self):
        from src.core.mesh_analyzers import detect_small_features_occ

        # sqrt(0.25) = 0.5 mm < 1.0 mm threshold
        feat = self._make_occ_feature(
            "planar_face", {}, [0.0, 0.0, 0.0], area_mm2=0.25
        )
        count, _, _ = detect_small_features_occ([feat], min_size_mm=1.0)
        assert count == 1

    def test_overlay_face_indices_found_in_nearby_mesh(self):
        """When a mesh is provided, face indices near the centroid are returned."""
        from src.core.mesh_analyzers import detect_small_features_occ

        # 4 mm box centered at origin — faces sit at ±2 mm from center.
        # area_mm2=25.0 → search_r = max(sqrt(25)=5, 1.5) = 5 mm > 2 mm → faces found.
        mesh = trimesh.creation.box([4, 4, 4])
        feat = self._make_occ_feature(
            "hole", {"radius_mm": 0.2}, [0.0, 0.0, 0.0], area_mm2=25.0
        )
        _, _, face_idx = detect_small_features_occ(
            [feat], min_size_mm=1.0, mesh=mesh
        )
        assert len(face_idx) > 0, "Expected nearby face indices to be returned"

    def test_empty_features_returns_zero(self):
        from src.core.mesh_analyzers import detect_small_features_occ

        count, centroids, faces = detect_small_features_occ([], min_size_mm=0.5)
        assert count == 0
        assert centroids == []
        assert faces == []


# ---------------------------------------------------------------------------
# Support-tower overlay
# ---------------------------------------------------------------------------


class TestSupportTowerOverlay:
    """generate_support_tower_overlay_glb and _generate_overlays_worker support-tower key."""

    def test_support_tower_overlay_generated_for_fdm_with_overhangs(self):
        """_generate_overlays_worker emits FDM__overhang_support when overhang faceIndices present."""
        from src.core.geometry import _generate_overlays_worker

        mesh = trimesh.creation.icosphere(radius=10)
        stl_bytes = mesh.export(file_type="stl")
        face_indices = list(range(min(10, len(mesh.faces))))

        reports = {
            "FDM": {
                "issues": [
                    {
                        "category": "overhang",
                        "faceIndices": face_indices,
                        "severity": "warning",
                        "title": "Overhang regions",
                        "description": "test",
                        "value": 0,
                        "threshold": 0,
                        "centroid": [0, 0, 0],
                    }
                ],
            }
        }

        result = _generate_overlays_worker(stl_bytes, reports)
        assert "FDM__overhang_support" in result, (
            f"Expected FDM__overhang_support key; got keys: {list(result.keys())}"
        )
        assert len(result["FDM__overhang_support"]) > 0

    def test_support_tower_overlay_absent_when_no_overhangs(self):
        """_generate_overlays_worker must NOT emit overhang_support when no overhang issues."""
        from src.core.geometry import _generate_overlays_worker

        mesh = trimesh.creation.box([10, 10, 10])
        stl_bytes = mesh.export(file_type="stl")

        reports = {
            "FDM": {
                "issues": [
                    {
                        "category": "thin_wall",
                        "faceIndices": [0, 1, 2],
                        "severity": "warning",
                        "title": "Thin walls",
                        "description": "test",
                        "value": 0,
                        "threshold": 0,
                        "centroid": [0, 0, 0],
                    }
                ],
            }
        }

        result = _generate_overlays_worker(stl_bytes, reports)
        assert "FDM__overhang_support" not in result

    def test_support_tower_prism_geometry(self):
        """GLB triangle count = 8 × face_count, prism bottoms reach mesh Z-min."""
        from src.core.overlay_generator import generate_support_tower_overlay_glb

        # Box at z=[0..10]: z_min=0 so prism bottoms reach y=0 after Z→Y rotation
        mesh = trimesh.creation.box([20, 20, 10])
        mesh.apply_translation([0, 0, 5])  # z range [0..10]

        face_indices = list(range(4))
        glb = generate_support_tower_overlay_glb(mesh, face_indices, reference_center=np.zeros(3))
        assert glb is not None
        assert len(glb) > 0

        loaded = trimesh.load(io.BytesIO(glb), file_type="glb")
        out_mesh = loaded.to_geometry() if isinstance(loaded, trimesh.Scene) else loaded

        # Each face produces exactly 8 prism triangles
        assert len(out_mesh.faces) == len(face_indices) * 8, (
            f"Expected {len(face_indices) * 8} triangles, got {len(out_mesh.faces)}"
        )

        # After Z→Y rotation, z=0 maps to y=0.
        # Prism bottoms (always at z_min=0) → Y-min ≈ 0 in the GLB.
        y_min = float(out_mesh.bounds[0, 1])
        assert abs(y_min) < 1.0, (
            f"GLB Y-min {y_min:.3f} mm should be near 0 (mesh Z-min after Z→Y rotation)"
        )


class TestDetectSmallFeaturesFallbackImproved:
    """The triangle-edge fallback must not flag smooth curved surfaces."""

    @staticmethod
    def _finely_tessellated_cylinder(radius: float = 20.0, height: float = 50.0,
                                     sections: int = 128) -> trimesh.Trimesh:
        """High-section cylinder → many tiny triangles on curved surface (false-positive source)."""
        cyl = trimesh.creation.cylinder(radius=radius, height=height, sections=sections)
        return cyl

    def test_smooth_cylinder_not_flagged_as_small_features(self):
        """A 20 mm radius cylinder should report 0 small features (no false positives)."""
        from src.core.mesh_analyzers import detect_small_features

        cyl = self._finely_tessellated_cylinder(radius=20.0, sections=128)
        # min_size_mm = 1.0; on a 20mm cylinder only genuine <1mm features should appear
        count, _, _ = detect_small_features(cyl, min_size_mm=1.0)
        assert count == 0, (
            f"Smooth 20mm cylinder reported {count} small features — tessellation false positive"
        )

    def test_tiny_box_is_still_detected(self):
        """A genuine 0.5 mm box should be detected as a small feature."""
        from src.core.mesh_analyzers import detect_small_features

        # Large base with a small protrusion on top (simplified via two meshes)
        large = trimesh.creation.box([20, 20, 20])
        small = trimesh.creation.box([0.4, 0.4, 0.4])
        small.apply_translation([0, 0, 10.2])  # sit on top
        combined = trimesh.util.concatenate([large, small])

        count, centroids, _ = detect_small_features(combined, min_size_mm=1.0)
        # The 0.4mm box faces should be detected.
        assert count >= 1, "Expected small 0.4mm box to be detected as a small feature"
