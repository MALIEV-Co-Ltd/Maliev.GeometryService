"""
Overlay GLB generator for DFM visualization.

Generates small GLB files that highlight specific face regions on the 3D model.
These overlays are loaded on-demand in the three.js viewer when a user clicks
a DFM issue in the overlay panel.

Coordinate conventions match _export_glb_worker in geometry.py:
- Input mesh is in mm, Z-up (all formats, including STEP/IGES via cascadio)
- Output GLB is Y-up (glTF spec), mm, centered
- Apply the same Z→Y pre-rotation so overlays align with the main GLB
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

import numpy as np
import numpy.typing as npt

logger = logging.getLogger(__name__)

# Z-up → Y-up pre-rotation matrix (same as _export_glb_worker)
# −90° around X: (x, y, z) → (x, z, −y)
_Z_TO_YUP = np.array(
    [[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=float
)

# Issue category → base RGBA colour (alpha = 160 / 255 ≈ 0.63)
# These are baked into vertex colours inside the GLB.
_CATEGORY_COLORS: dict[str, tuple[int, int, int, int]] = {
    "thin_wall": (255, 100, 0, 160),  # orange (gradient overrides this)
    "unsupported_wall": (255, 165, 0, 160),  # orange
    "overhang": (255, 230, 0, 160),  # yellow
    "hole": (0, 120, 255, 160),  # blue
    "internal_radius": (230, 0, 230, 160),  # magenta
    "cavity_depth": (230, 50, 50, 160),  # red
    "chatter_risk": (255, 140, 0, 160),  # orange
    "tool_deflection": (180, 0, 0, 160),  # dark red
    "bridge": (0, 200, 180, 160),  # teal
    "escape_hole": (0, 200, 200, 160),  # cyan
    "small_feature": (255, 150, 200, 160),  # pink
    "thread": (128, 0, 200, 160),  # purple
    "undercut": (100, 0, 160, 160),  # dark purple
    "pin": (255, 80, 180, 160),  # hot pink
    "sharp_corner": (200, 0, 100, 160),  # crimson
    "resin_trap": (0, 200, 200, 160),  # cyan
    "suction": (100, 150, 255, 160),  # light blue
    "hollow": (0, 200, 100, 160),  # green
    "default": (200, 200, 0, 160),  # yellow-green
}


def _lerp_color(
    c1: tuple[int, int, int, int],
    c2: tuple[int, int, int, int],
    t: float,
) -> tuple[int, int, int, int]:
    """Linearly interpolate between two RGBA colours. t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
        int(c1[3] + (c2[3] - c1[3]) * t),
    )


def _thin_wall_gradient_color(
    severity: float,  # 0.0 = at threshold, 1.0 = very thin (most severe)
) -> tuple[int, int, int, int]:
    """Return orange→red gradient colour based on thin-wall severity."""
    orange = (255, 165, 0, 160)
    red = (220, 20, 0, 160)
    return _lerp_color(orange, red, severity)


def generate_overlay_glb(
    mesh: object,  # trimesh.Trimesh
    face_indices: list[int],
    category: str,
    reference_center: npt.NDArray[Any] | None = None,
    severity_per_face: dict[int, float] | None = None,
) -> bytes | None:
    """Extract faces from *mesh* by index, apply vertex colours, export GLB.

    The resulting GLB uses the same coordinate transform and centering as the
    main viewer GLB so overlays align perfectly in the scene.

    Args:
        mesh:              Source trimesh.Trimesh (Z-up, mm).
        face_indices:      Triangle indices to include in the overlay.
        category:          Issue category key (determines base colour).
        reference_center:  Centre-of-mass used when the main GLB was exported.
                           Pass the same value for pixel-perfect alignment.
        severity_per_face: Optional per-face severity in [0, 1]. Used for the
                           thin-wall orange→red gradient.

    Returns:
        GLB bytes or None on failure.
    """
    import trimesh

    try:
        if not isinstance(mesh, trimesh.Trimesh):
            return None
        if not face_indices:
            return None

        # Clamp indices to valid range
        max_idx = len(mesh.faces) - 1
        valid_indices = [f for f in face_indices if 0 <= f <= max_idx]
        if not valid_indices:
            return None

        # Extract sub-mesh
        submesh = mesh.submesh([valid_indices], append=True)
        if not isinstance(submesh, trimesh.Trimesh) or len(submesh.vertices) == 0:
            return None

        # Build per-face RGBA colours
        base_color = _CATEGORY_COLORS.get(category, _CATEGORY_COLORS["default"])

        face_colors = np.zeros((len(submesh.faces), 4), dtype=np.uint8)
        for i, orig_fidx in enumerate(valid_indices[: len(submesh.faces)]):
            if category == "thin_wall" and severity_per_face is not None:
                sev = severity_per_face.get(orig_fidx, 0.5)
                color = _thin_wall_gradient_color(sev)
            else:
                color = base_color
            face_colors[i] = color

        submesh.visual = trimesh.visual.ColorVisuals(
            mesh=submesh,
            face_colors=face_colors,
        )

        # Apply same centering as main GLB
        center = reference_center if reference_center is not None else mesh.center_mass
        submesh.apply_translation(-center)

        # Z-up → Y-up (same pre-rotation as _export_glb_worker)
        submesh.apply_transform(_Z_TO_YUP)

        return cast(bytes, submesh.export(file_type="glb"))

    except Exception as exc:
        logger.warning(
            "overlay GLB generation failed for category=%s: %s", category, exc
        )
        return None


def generate_multi_body_overlay_glb(
    bodies: Sequence[object],  # list[trimesh.Trimesh]
    reference_center: npt.NDArray[Any] | None = None,
) -> bytes | None:
    """Build a single GLB where each body is tinted a distinct colour.

    Bodies are coloured in sequence from a fixed palette (red, blue, green,
    magenta, cyan, orange, yellow, purple) so viewers can tell them apart.
    The GLB uses the same centering and Z→Y rotation as the main viewer GLB.

    Args:
        bodies:           List of trimesh.Trimesh objects in Z-up, mm.
        reference_center: Centre-of-mass of the full concatenated mesh.
                          Pass the same value used for the main GLB export.

    Returns:
        GLB bytes or None on failure.
    """
    import trimesh

    _BODY_PALETTE: list[tuple[int, int, int, int]] = [  # noqa: N806
        (220, 40, 40, 210),  # red
        (40, 80, 220, 210),  # blue
        (30, 160, 30, 210),  # green
        (180, 0, 180, 210),  # magenta
        (0, 180, 180, 210),  # cyan
        (220, 120, 0, 210),  # orange
        (180, 180, 0, 210),  # yellow
        (100, 0, 180, 210),  # purple
    ]

    try:
        if not bodies:
            return None

        colored: list[object] = []
        for i, body in enumerate(bodies):
            if not isinstance(body, trimesh.Trimesh) or len(body.vertices) == 0:
                continue
            color = _BODY_PALETTE[i % len(_BODY_PALETTE)]
            face_colors = np.full((len(body.faces), 4), color, dtype=np.uint8)
            body = body.copy()
            body.visual = trimesh.visual.ColorVisuals(
                mesh=body, face_colors=face_colors
            )
            colored.append(body)

        if not colored:
            return None

        combined = trimesh.util.concatenate(colored)
        if not isinstance(combined, trimesh.Trimesh) or len(combined.vertices) == 0:
            return None

        center = (
            reference_center if reference_center is not None else combined.center_mass
        )
        combined.apply_translation(-center)
        combined.apply_transform(_Z_TO_YUP)

        return cast(bytes, combined.export(file_type="glb"))

    except Exception as exc:
        logger.warning("multi-body overlay GLB generation failed: %s", exc)
        return None


def generate_support_tower_overlay_glb(
    mesh: object,  # trimesh.Trimesh
    face_indices: list[int],
    reference_center: npt.NDArray[Any] | None = None,
    grid_spacing_mm: float = 2.0,
    wall_half: float = 0.2,
    surface_offset_mm: float = 0.08,
) -> bytes | None:
    """Build a projected support mockup GLB from overhanging faces.

    The support overlay is intentionally a translucent mockup, not generated
    slicer support geometry. It projects the selected overhang footprint down
    to the build plate and adds vertical perimeter walls so users see where
    support material would sit, instead of seeing the same highlighted part
    faces duplicated as "support".

    The resulting GLB uses the same coordinate transform as generate_overlay_glb
    so it aligns with the main model.  OVERLAY_STYLES in part-viewer.js controls
    colour and opacity — no vertex colours are baked in.

    Args:
        mesh:             Source trimesh.Trimesh (Z-up, mm).
        face_indices:     Overhang triangle indices.
        reference_center: Centre used when the main GLB was exported.
        grid_spacing_mm:  Legacy parameter; ignored for face-following mockups.
        wall_half:        Legacy parameter; ignored for face-following mockups.
        surface_offset_mm: Small outward offset so the translucent support layer
                           does not z-fight with the red overhang overlay.

    Returns:
        GLB bytes or None on failure.
    """
    import trimesh

    _ = (grid_spacing_mm, wall_half, surface_offset_mm)

    try:
        if not isinstance(mesh, trimesh.Trimesh):
            return None
        if not face_indices:
            return None

        max_idx = len(mesh.faces) - 1
        valid_indices = [f for f in face_indices if 0 <= f <= max_idx]
        if not valid_indices:
            return None

        source_vertices = np.asarray(mesh.vertices, dtype=float)
        source_faces = np.asarray(mesh.faces, dtype=int)
        selected_faces = source_faces[valid_indices]
        build_z = float(source_vertices[:, 2].min())

        support_vertices: list[list[float]] = []
        support_faces: list[list[int]] = []

        def triangle_area(points: npt.NDArray[Any]) -> float:
            return float(
                np.linalg.norm(np.cross(points[1] - points[0], points[2] - points[0]))
                * 0.5
            )

        def add_vertex(point: npt.NDArray[Any]) -> int:
            support_vertices.append(
                [float(point[0]), float(point[1]), float(point[2])]
            )
            return len(support_vertices) - 1

        def add_triangle(
            p0: npt.NDArray[Any],
            p1: npt.NDArray[Any],
            p2: npt.NDArray[Any],
        ) -> None:
            points = np.array([p0, p1, p2], dtype=float)
            if triangle_area(points) < 1e-8:
                return
            support_faces.append(
                [add_vertex(points[0]), add_vertex(points[1]), add_vertex(points[2])]
            )

        def add_quad(
            p0: npt.NDArray[Any],
            p1: npt.NDArray[Any],
            p2: npt.NDArray[Any],
            p3: npt.NDArray[Any],
        ) -> None:
            add_triangle(p0, p1, p2)
            add_triangle(p0, p2, p3)

        # Bottom footprint: project each selected overhang triangle onto the
        # build plate. Keeping the original triangle footprint preserves curved
        # boundaries without spanning across disconnected support regions.
        for face in selected_faces:
            tri = source_vertices[face]
            bottom = tri.copy()
            bottom[:, 2] = build_z
            add_triangle(bottom[0], bottom[2], bottom[1])

        # Vertical perimeter walls: only boundary edges of the selected overhang
        # region should create walls. Interior edges would create hidden duplicate
        # sheets inside the mockup and make the overlay visually noisy.
        edge_counts: dict[tuple[int, int], int] = {}
        for face in selected_faces:
            for idx in range(3):
                a = int(face[idx])
                b = int(face[(idx + 1) % 3])
                key = (min(a, b), max(a, b))
                edge_counts[key] = edge_counts.get(key, 0) + 1

        for edge, count in edge_counts.items():
            if count != 1:
                continue
            top0 = source_vertices[edge[0]].copy()
            top1 = source_vertices[edge[1]].copy()
            base0 = top0.copy()
            base1 = top1.copy()
            base0[2] = build_z
            base1[2] = build_z
            if (
                np.linalg.norm(top0 - base0) < 1e-8
                and np.linalg.norm(top1 - base1) < 1e-8
            ):
                continue
            add_quad(top0, top1, base1, base0)

        if not support_vertices or not support_faces:
            return None

        support_mesh = trimesh.Trimesh(
            vertices=np.asarray(support_vertices, dtype=float),
            faces=np.asarray(support_faces, dtype=int),
            process=False,
        )
        if (
            not isinstance(support_mesh, trimesh.Trimesh)
            or len(support_mesh.vertices) == 0
        ):
            return None
        support_mesh.update_faces(support_mesh.nondegenerate_faces())
        support_mesh.remove_unreferenced_vertices()
        support_mesh.merge_vertices()
        if len(support_mesh.faces) == 0:
            return None

        center = reference_center if reference_center is not None else mesh.center_mass
        support_mesh.apply_translation(-center)
        support_mesh.apply_transform(_Z_TO_YUP)

        return cast(bytes, support_mesh.export(file_type="glb"))

    except Exception as exc:
        logger.warning("support tower overlay GLB generation failed: %s", exc)
        return None


def get_category_color_hex(category: str) -> str:
    """Return the CSS hex colour for a DFM category (for frontend use)."""
    rgba = _CATEGORY_COLORS.get(category, _CATEGORY_COLORS["default"])
    return f"#{rgba[0]:02x}{rgba[1]:02x}{rgba[2]:02x}"
