"""
Overlay GLB generator for DFM visualization.

Generates small GLB files that highlight specific face regions on the 3D model.
These overlays are loaded on-demand in the BabylonJS viewer when a user clicks
a DFM issue in the overlay panel.

Coordinate conventions match _export_glb_worker in geometry.py:
- Input mesh is in mm, Z-up (all formats, including STEP/IGES via cascadio)
- Output GLB is Y-up (glTF spec), mm, centered
- Apply the same Z→Y pre-rotation so overlays align with the main GLB
"""

from __future__ import annotations

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
    reference_center: np.ndarray | None = None,
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

        glb_bytes: bytes = submesh.export(file_type="glb")
        return glb_bytes

    except Exception as exc:
        logger.warning(
            "overlay GLB generation failed for category=%s: %s", category, exc
        )
        return None


def generate_multi_body_overlay_glb(
    bodies: list,  # list[trimesh.Trimesh]
    reference_center: np.ndarray | None = None,
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

        colored: list = []
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

        glb_bytes: bytes = combined.export(file_type="glb")
        return glb_bytes

    except Exception as exc:
        logger.warning("multi-body overlay GLB generation failed: %s", exc)
        return None


def generate_support_tower_overlay_glb(
    mesh: object,  # trimesh.Trimesh
    face_indices: list[int],
    reference_center: np.ndarray | None = None,
    grid_spacing_mm: float = 2.0,
    wall_half: float = 0.2,
) -> bytes | None:
    """Build a rectilinear-grid support structure GLB from overhanging faces.

    Creates two sets of thin walls: X-direction walls at fixed Y intervals and
    Y-direction walls at fixed X intervals, spaced grid_spacing_mm apart.  Each
    wall gets its own base height from a per-wall downward ray cast so pillars
    hug the nearest upward-facing mesh surface below rather than starting from
    the build plate, correctly handling stacked horizontal holes.

    The resulting GLB uses the same coordinate transform as generate_overlay_glb
    so it aligns with the main model.  OVERLAY_STYLES in part-viewer.js controls
    colour and opacity — no vertex colours are baked in.

    Args:
        mesh:             Source trimesh.Trimesh (Z-up, mm).
        face_indices:     Overhang triangle indices.
        reference_center: Centre used when the main GLB was exported.
        grid_spacing_mm:  Spacing between adjacent parallel walls in mm.
        wall_half:        Half-thickness of each wall cross-section in mm.

    Returns:
        GLB bytes or None on failure.
    """
    import trimesh

    SUPPORT_CLEARANCE = 0.2  # mm gap above mesh face so pillar doesn't z-fight

    try:
        if not isinstance(mesh, trimesh.Trimesh):
            return None
        if not face_indices:
            return None

        max_idx = len(mesh.faces) - 1
        valid_indices = [f for f in face_indices if 0 <= f <= max_idx]
        if not valid_indices:
            return None

        z_min = float(mesh.vertices[:, 2].min())
        face_verts = mesh.vertices[mesh.faces[valid_indices]]  # (N, 3, 3)

        all_xy = face_verts[:, :, :2].reshape(-1, 2)
        xy_min = all_xy.min(axis=0)
        xy_max = all_xy.max(axis=0)

        z_top = float(face_verts.mean(axis=1)[:, 2].mean())
        x_mid = float((xy_min[0] + xy_max[0]) / 2)
        y_mid = float((xy_min[1] + xy_max[1]) / 2)

        # Build intersector once for per-wall ray queries.
        overhang_set = set(valid_indices)
        fn = mesh.face_normals
        try:
            intersector = trimesh.ray.ray_triangle.RayMeshIntersector(mesh)
        except Exception:
            intersector = None

        def _ray_z_bot(ox: float, oy: float) -> float:
            """Downward ray at (ox, oy) → highest upward face below z_top."""
            if intersector is None:
                return z_min
            try:
                locs, _, face_idx_hit = intersector.intersects_location(
                    ray_origins=np.array([[ox, oy, z_top + 0.1]]),
                    ray_directions=np.array([[0.0, 0.0, -1.0]]),
                    multiple_hits=True,
                )
                best = z_min
                for loc, fi in zip(locs, face_idx_hit, strict=False):
                    if fi in overhang_set:
                        continue
                    if fn[fi][2] <= 0.1:  # skip downward/vertical faces
                        continue
                    hz = float(loc[2])
                    if hz < z_top - 0.05 and hz > best:
                        best = hz
                return best + SUPPORT_CLEARANCE if best > z_min else z_min
            except Exception:
                return z_min

        # Grid lines for X-direction walls (at fixed Y) and Y-direction walls (at fixed X).  # noqa: E501
        y_lines = np.arange(
            xy_min[1], xy_max[1] + grid_spacing_mm * 0.5, grid_spacing_mm
        )
        x_lines = np.arange(
            xy_min[0], xy_max[0] + grid_spacing_mm * 0.5, grid_spacing_mm
        )
        if len(y_lines) == 0:
            y_lines = np.array([y_mid])
        if len(x_lines) == 0:
            x_lines = np.array([x_mid])

        # Wall extents (full overhang span + tiny margin for clean edges).
        x_lo = float(xy_min[0]) - wall_half
        x_hi = float(xy_max[0]) + wall_half
        y_lo_full = float(xy_min[1]) - wall_half
        y_hi_full = float(xy_max[1]) + wall_half

        # Box topology: 8 verts, 12 triangles (6 quads × 2 tris).
        local_tris = np.array(
            [
                [0, 1, 2],
                [0, 2, 3],  # top cap
                [4, 6, 5],
                [4, 7, 6],  # bot cap (flipped winding)
                [0, 4, 5],
                [0, 5, 1],  # -Y side
                [1, 5, 6],
                [1, 6, 2],  # +X side
                [2, 6, 7],
                [2, 7, 3],  # +Y side
                [3, 7, 4],
                [3, 4, 0],  # -X side
            ],
            dtype=np.int64,
        )

        all_verts_list: list = []
        all_faces_list: list = []
        vert_offset = 0

        def _add_wall(
            x0: float, x1: float, y0: float, y1: float, z_bot: float
        ) -> None:
            nonlocal vert_offset
            if z_top - z_bot < 0.1:
                return  # pillar too short to render
            verts = np.array(
                [
                    [x0, y0, z_top],
                    [x1, y0, z_top],
                    [x1, y1, z_top],
                    [x0, y1, z_top],
                    [x0, y0, z_bot],
                    [x1, y0, z_bot],
                    [x1, y1, z_bot],
                    [x0, y1, z_bot],
                ],
                dtype=float,
            )
            all_verts_list.append(verts)
            all_faces_list.append(local_tris + vert_offset)
            vert_offset += 8

        # X-direction walls: span full X range, thin in Y. Ray at x_mid for each y.
        for y_pos in y_lines:
            local_z = _ray_z_bot(x_mid, float(y_pos))
            _add_wall(x_lo, x_hi, float(y_pos) - wall_half, float(y_pos) + wall_half, local_z)

        # Y-direction walls: span full Y range, thin in X. Ray at y_mid for each x.
        for x_pos in x_lines:
            local_z = _ray_z_bot(float(x_pos), y_mid)
            _add_wall(
                float(x_pos) - wall_half, float(x_pos) + wall_half, y_lo_full, y_hi_full, local_z
            )

        if not all_verts_list:
            return None

        all_verts = np.vstack(all_verts_list)
        all_faces = np.vstack(all_faces_list)

        support_mesh = trimesh.Trimesh(
            vertices=all_verts, faces=all_faces, process=False
        )

        center = reference_center if reference_center is not None else mesh.center_mass
        support_mesh.apply_translation(-center)
        support_mesh.apply_transform(_Z_TO_YUP)

        return support_mesh.export(file_type="glb")

    except Exception as exc:
        logger.warning("support tower overlay GLB generation failed: %s", exc)
        return None


def get_category_color_hex(category: str) -> str:
    """Return the CSS hex colour for a DFM category (for frontend use)."""
    rgba = _CATEGORY_COLORS.get(category, _CATEGORY_COLORS["default"])
    return f"#{rgba[0]:02x}{rgba[1]:02x}{rgba[2]:02x}"
