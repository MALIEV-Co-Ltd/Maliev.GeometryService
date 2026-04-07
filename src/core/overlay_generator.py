"""
Overlay GLB generator for DFM visualization.

Generates small GLB files that highlight specific face regions on the 3D model.
These overlays are loaded on-demand in the BabylonJS viewer when a user clicks
a DFM issue in the overlay panel.

Coordinate conventions match _export_glb_worker in geometry.py:
- Input mesh is in mm, Z-up
- Output GLB is Y-up (glTF spec), mm
- Apply the same Z→Y pre-rotation so overlays align with the main GLB
"""

from __future__ import annotations

import io
import logging

import numpy as np

logger = logging.getLogger(__name__)

# Z-up → Y-up pre-rotation matrix (same as _export_glb_worker)
# −90° around X: (x, y, z) → (x, z, −y)
_Z_TO_YUP = np.array(
    [[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=float
)

# Issue category → base RGBA colour (alpha = 160 / 255 ≈ 0.63)
# These are baked into vertex colours inside the GLB.
_CATEGORY_COLORS: dict[str, tuple[int, int, int, int]] = {
    "thin_wall":        (255, 100,   0, 160),  # orange (gradient overrides this)
    "unsupported_wall": (255, 165,   0, 160),  # orange
    "overhang":         (255, 230,   0, 160),  # yellow
    "hole":             (  0, 120, 255, 160),  # blue
    "internal_radius":  (230,   0, 230, 160),  # magenta
    "cavity_depth":     (230,  50,  50, 160),  # red
    "chatter_risk":     (255, 140,   0, 160),  # orange
    "tool_deflection":  (180,   0,   0, 160),  # dark red
    "bridge":           (  0, 200, 180, 160),  # teal
    "escape_hole":      (  0, 200, 200, 160),  # cyan
    "small_feature":    (255, 150, 200, 160),  # pink
    "thread":           (128,   0, 200, 160),  # purple
    "undercut":         (100,   0, 160, 160),  # dark purple
    "pin":              (255,  80, 180, 160),  # hot pink
    "sharp_corner":     (200,   0, 100, 160),  # crimson
    "resin_trap":       (  0, 200, 200, 160),  # cyan
    "suction":          (100, 150, 255, 160),  # light blue
    "hollow":           (  0, 200, 100, 160),  # green
    "default":          (200, 200,   0, 160),  # yellow-green
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
    red    = (220,  20, 0, 160)
    return _lerp_color(orange, red, severity)


def generate_overlay_glb(
    mesh: object,  # trimesh.Trimesh
    face_indices: list[int],
    category: str,
    reference_center: "np.ndarray | None" = None,
    severity_per_face: dict[int, float] | None = None,
    output_in_meters: bool = False,
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
        for i, orig_fidx in enumerate(valid_indices[:len(submesh.faces)]):
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

        # For STEP/IGES files the main viewer GLB is in meters (cascadio output).
        # Babylon applies scaleFactor ≈ 1000 to both the main model and overlays.
        # Convert overlay from mm → meters so Babylon's rescaling yields the correct scale.
        if output_in_meters:
            submesh.apply_scale(0.001)

        glb_bytes: bytes = submesh.export(file_type="glb")
        return glb_bytes

    except Exception as exc:
        logger.warning("overlay GLB generation failed for category=%s: %s",
                       category, exc)
        return None


def get_category_color_hex(category: str) -> str:
    """Return the CSS hex colour for a DFM category (for frontend use)."""
    rgba = _CATEGORY_COLORS.get(category, _CATEGORY_COLORS["default"])
    return "#{:02x}{:02x}{:02x}".format(rgba[0], rgba[1], rgba[2])
