"""Regression tests for DFM crash caused by degenerate mesh vertices.

These tests guard against the trimesh.geometry.vertex_face_indices
ValueError: "NumPy boolean array indexing assignment cannot assign X input
values to Y output values where the mask is true", which occurs when a mesh
has unreferenced (dead) vertices that cause a vertex/face count mismatch.

See: src/core/mesh_analyzers.py detect_holes_mesh and
     src/core/geometry.py _analyze_single_process / _analyze_single_body
"""

import io

import numpy as np
import pytest
import trimesh

from src.core.mesh_analyzers import detect_holes_mesh


def make_degenerate_mesh(seed: int = 42) -> trimesh.Trimesh:
    """Create a cube mesh with extra unreferenced vertices appended.

    The extra vertices are never referenced by any face, which historically
    caused trimesh.geometry.vertex_face_indices to raise a ValueError.
    """
    rng = np.random.default_rng(seed)
    box = trimesh.creation.box()
    extra_verts = rng.random((1000, 3)) * 0.001  # unreferenced garbage verts
    vertices = np.vstack([box.vertices, extra_verts])
    mesh = trimesh.Trimesh(vertices=vertices, faces=box.faces, process=False)
    return mesh


class TestDetectHolesDegenerateMesh:
    """detect_holes_mesh must not raise on meshes with unreferenced vertices."""

    def test_returns_list_not_raises(self):
        """detect_holes_mesh on a degenerate mesh returns a list."""
        mesh = make_degenerate_mesh()
        result = detect_holes_mesh(mesh, min_diameter_mm=0.1)
        assert isinstance(result, list), (
            "Expected detect_holes_mesh to return a list, not raise"
        )

    def test_fallback_triggered_via_monkeypatch(self, monkeypatch):
        """Fallback adjacency build is exercised when vertex_faces raises ValueError.

        This directly exercises the try/except fallback added in Fix 2,
        independent of whether the degenerate mesh actually triggers the bug
        in the installed trimesh version.
        """
        mesh = make_degenerate_mesh()

        # Patch the property to raise the exact error seen in production
        original_prop = type(mesh).vertex_faces.fget  # noqa: B009

        def _raise_vertex_faces(self):  # noqa: ANN001
            raise ValueError(
                "NumPy boolean array indexing assignment cannot assign "
                "184476 input values to 199446 output values where the mask is true"
            )

        monkeypatch.setattr(type(mesh), "vertex_faces", property(_raise_vertex_faces))

        result = detect_holes_mesh(mesh, min_diameter_mm=0.1)
        assert isinstance(result, list), (
            "Expected fallback adjacency build to return a list, not raise"
        )


class TestMeshSanitizationOnLoad:
    """Verify that degenerate meshes are sanitized before DFM analysis."""

    def test_sanitize_removes_unreferenced_vertices(self):
        """After sanitization a mesh has no unreferenced vertices."""
        mesh = make_degenerate_mesh()
        pre_vertex_count = len(mesh.vertices)

        # Apply the same sanitization sequence used in geometry.py
        sanitized = mesh.copy()
        sanitized.process(validate=True)
        sanitized.remove_unreferenced_vertices()
        sanitized.merge_vertices()
        sanitized.update_faces(sanitized.unique_faces())
        sanitized.update_faces(sanitized.nondegenerate_faces())

        post_vertex_count = len(sanitized.vertices)
        # Sanitized mesh must have fewer vertices (the dead ones are gone)
        assert post_vertex_count < pre_vertex_count, (
            f"Expected vertex count to decrease after sanitization "
            f"(was {pre_vertex_count}, got {post_vertex_count})"
        )
        # Face count must remain sane
        assert len(sanitized.faces) > 0, "Sanitized mesh must still have faces"

    def test_vertex_faces_stable_after_sanitization(self):
        """vertex_faces no longer raises after sanitization."""
        mesh = make_degenerate_mesh()

        sanitized = mesh.copy()
        sanitized.process(validate=True)
        sanitized.remove_unreferenced_vertices()
        sanitized.merge_vertices()
        sanitized.update_faces(sanitized.unique_faces())
        sanitized.update_faces(sanitized.nondegenerate_faces())

        # Must not raise
        vf = sanitized.vertex_faces
        assert vf is not None

    def test_analyze_single_process_degenerate_stl(self):
        """_analyze_single_process completes without error on a degenerate mesh."""
        # Build degenerate mesh and serialise to STL bytes
        mesh = make_degenerate_mesh()
        stl_buf = io.BytesIO()
        mesh.export(stl_buf, file_type="stl")
        stl_bytes = stl_buf.getvalue()

        from src.core.geometry import _analyze_single_process

        result = _analyze_single_process(stl_bytes, "FDM")
        # Must return a dict (possibly with issues, possibly empty) — not raise
        assert isinstance(result, dict), (
            f"Expected dict from _analyze_single_process, got {type(result)}"
        )
        # Must not have an error_type unless it's a legitimate validation error
        # (EmptyMesh is acceptable; a crash-style error is not)
        error_type = result.get("error_type")
        crash_errors = {"ValueError", "IndexError", "AttributeError", "RuntimeError"}
        assert error_type not in crash_errors, (
            f"_analyze_single_process returned crash error: {error_type}: "
            f"{result.get('message')}"
        )
