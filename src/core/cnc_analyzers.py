"""
CNC-specific DFM analysis functions for milling and turning operations.

All length values are in mm. These functions operate on tessellated meshes
(trimesh.Trimesh) and produce structured results including face indices
for overlay GLB generation.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import numpy.typing as npt

from .dfm_models import (
    AxisSymmetryReport,
    CavityFeature,
    GrooveFeature,
    InternalRadiusFeature,
    ToolAccessReport,
)
from .dfm_thresholds import MILLING_RULES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal radius / corner detection
# ---------------------------------------------------------------------------


def detect_internal_radii(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
) -> list[InternalRadiusFeature]:
    """Detect concave corners and estimate their fillet radius.

    Strategy:
    1. Find edges shared by two faces with a concave dihedral angle
       (interior angle < 180° — i.e. the normals diverge inward).
    2. For each concave edge, cluster nearby concave edges into a region
       and estimate a local billet radius from edge curvature.

    Returns a list of InternalRadiusFeature objects (one per distinct corner).
    """
    import trimesh

    results: list[InternalRadiusFeature] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 4:
            return results

        # Build edge → [face indices] mapping
        edge_to_faces: dict[tuple[int, int], list[int]] = {}
        for fidx, face in enumerate(mesh.faces):
            for k in range(3):
                key = (
                    min(int(face[k]), int(face[(k + 1) % 3])),
                    max(int(face[k]), int(face[(k + 1) % 3])),
                )
                edge_to_faces.setdefault(key, []).append(fidx)

        face_normals = mesh.face_normals
        face_centroids = mesh.triangles_center
        vertices = mesh.vertices

        concave_edges: list[tuple[tuple[int, int], np.ndarray]] = []

        for (v0, v1), faces in edge_to_faces.items():
            if len(faces) != 2:
                continue
            fi, fj = faces[0], faces[1]
            ni, nj = face_normals[fi], face_normals[fj]
            ci, cj = face_centroids[fi], face_centroids[fj]

            # Vector from face i centroid toward face j centroid
            diff = cj - ci
            # If both normals point away from the shared edge midpoint → convex
            # If one points toward the edge (dot product negative with diff) → concave
            dot_i = float(np.dot(ni, diff))
            dot_j = float(np.dot(nj, -diff))

            is_concave = dot_i < -0.05 and dot_j < -0.05
            if not is_concave:
                continue

            edge_mid = (vertices[v0] + vertices[v1]) / 2.0
            concave_edges.append(((v0, v1), edge_mid))

        if not concave_edges:
            return results

        # Cluster concave edges by proximity
        all_mids = np.array([m for _, m in concave_edges])
        cluster_radius = 3.0  # mm — group nearby concave edges into one feature
        used: set[int] = set()

        for i, (_edge_key, mid_i) in enumerate(concave_edges):
            if i in used:
                continue

            distances = np.linalg.norm(all_mids - mid_i, axis=1)
            cluster_mask = distances < cluster_radius
            cluster_indices = np.where(cluster_mask)[0]

            for idx in cluster_indices:
                used.add(int(idx))

            cluster_mids = all_mids[cluster_mask]
            centroid = cluster_mids.mean(axis=0)

            # Estimate local radius from curvature of edge arc
            # Approximate: radius ≈ 0 for sharp corners (no fillet)
            # For fillets, the adjacent triangles span a distance ≈ 2×radius
            span = float(
                np.linalg.norm(cluster_mids.max(axis=0) - cluster_mids.min(axis=0))
            )
            estimated_radius = span / 2.0

            # Gather face indices for overlay
            face_idxs: list[int] = []
            for eidx in cluster_indices:
                ek = concave_edges[int(eidx)][0]
                face_idxs.extend(edge_to_faces.get(ek, []))

            results.append(
                InternalRadiusFeature(
                    radius_mm=estimated_radius,
                    centroid=[
                        float(centroid[0]),
                        float(centroid[1]),
                        float(centroid[2]),
                    ],
                    depth_mm=0.0,  # depth filled in by cavity analysis
                    face_indices=list(set(face_idxs))[:50],
                )
            )

    except Exception as exc:
        logger.warning("internal radius detection failed: %s", exc)

    return results[:100]


# ---------------------------------------------------------------------------
# Cavity / pocket detection
# ---------------------------------------------------------------------------


def detect_cavities(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
) -> list[CavityFeature]:
    """Detect pocket / cavity features and measure depth vs. width.

    Strategy:
    1. Find downward-facing flat floor faces (normal.z < -0.7).
    2. For each floor face cluster, cast a ray upward to find the cavity
       opening (the first face hit above).
    3. Measure depth (floor → opening) and width (cluster bounding box).

    Returns:
        List of CavityFeature objects.
    """
    import trimesh

    cavities: list[CavityFeature] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 6:
            return cavities

        face_normals = mesh.face_normals
        face_centroids = mesh.triangles_center

        # Find floor-like faces: normal pointing down (Z < -0.7) or up (Z > 0.7)
        # Pockets open upward, so the floor faces downward in the model space
        # (normals point into the cavity = downward for a top-open pocket)
        floor_mask = face_normals[:, 2] < -0.7
        if not np.any(floor_mask):
            return cavities

        floor_indices = np.where(floor_mask)[0]

        # Cluster floor faces by XY proximity
        floor_centroids = face_centroids[floor_indices]
        cluster_dist = 5.0  # mm

        used: set[int] = set()
        for i in range(len(floor_indices)):
            if i in used:
                continue
            int(floor_indices[i])
            ci = floor_centroids[i]
            dists = np.linalg.norm(floor_centroids - ci, axis=1)
            cluster_mask = dists < cluster_dist
            cluster_local_indices = np.where(cluster_mask)[0]

            for idx in cluster_local_indices:
                used.add(int(idx))

            cluster_face_indices = floor_indices[cluster_local_indices].tolist()
            cluster_centroids = floor_centroids[cluster_mask]

            # Bounding box of cluster = cavity width
            extents = cluster_centroids.max(axis=0) - cluster_centroids.min(axis=0)
            width = float(max(extents[0], extents[1]))
            if width < 0.5:
                continue

            centroid = cluster_centroids.mean(axis=0)

            # Ray upward from centroid to find cavity top
            ray_origin = centroid.reshape(1, 3)
            ray_dir = np.array([[0.0, 0.0, 1.0]])

            try:
                locs, _, _ = mesh.ray.intersects_location(
                    ray_origin, ray_dir, multiple_hits=True
                )
                if len(locs) == 0:
                    continue
                # First hit above centroid
                locs_above = locs[locs[:, 2] > float(centroid[2])]
                if len(locs_above) == 0:
                    continue
                top_z = float(locs_above[:, 2].min())
                depth = top_z - float(centroid[2])
            except Exception:
                continue

            if depth < 1.0:
                continue

            cavities.append(
                CavityFeature(
                    width_mm=max(width, 1.0),
                    depth_mm=depth,
                    centroid=[
                        float(centroid[0]),
                        float(centroid[1]),
                        float(centroid[2]),
                    ],
                    face_indices=[int(f) for f in cluster_face_indices[:50]],
                )
            )

    except Exception as exc:
        logger.warning("cavity detection failed: %s", exc)

    return cavities[:50]


# ---------------------------------------------------------------------------
# Tool access (3/4/5-axis) analysis
# ---------------------------------------------------------------------------


def detect_tool_access(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
) -> ToolAccessReport:
    """Determine the minimum number of CNC axes required to reach all faces.

    Approach:
    - 3-axis: every face normal has a positive or zero Z component, or can
      be reached from a fixed setup orientation.
    - 4-axis: faces requiring rotation around X or Y (but not Z).
    - 5-axis: any face normal that cannot be reached with 4-axis.

    Uses face normal visibility: a face is accessible if its outward normal
    has a positive component along some machining direction.

    Returns a ToolAccessReport.
    """
    import trimesh

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 3:
            return ToolAccessReport(
                minimum_axes=3,
                inaccessible_face_count=0,
                inaccessible_face_indices=[],
                details="Mesh too simple to analyse",
            )

        normals = mesh.face_normals  # (N, 3)

        # 3-axis setup directions (6 standard orientations)
        dirs_3axis = np.array(
            [
                [0, 0, 1],
                [0, 0, -1],
                [1, 0, 0],
                [-1, 0, 0],
                [0, 1, 0],
                [0, -1, 0],
            ],
            dtype=float,
        )

        # A face is accessible in 3-axis if any standard direction yields
        # dot product > 0 with the face normal (normal points toward tool)
        accessible_3axis = np.any(normals @ dirs_3axis.T > 0.1, axis=1)

        if np.all(accessible_3axis):
            return ToolAccessReport(
                minimum_axes=3,
                inaccessible_face_count=0,
                inaccessible_face_indices=[],
                details="All faces reachable in 3-axis setup",
            )

        inaccessible_after_3 = np.where(~accessible_3axis)[0]

        # 4-axis: add directions obtained by rotating around Z (arbitrary angle)
        # Approximate: faces with normals mostly in XY plane are 4-axis reachable
        inaccessible_normals = normals[inaccessible_after_3]
        xy_component = np.linalg.norm(inaccessible_normals[:, :2], axis=1)
        z_component = np.abs(inaccessible_normals[:, 2])
        is_4axis_reachable = xy_component > z_component

        inaccessible_after_4 = inaccessible_after_3[~is_4axis_reachable]

        if len(inaccessible_after_4) == 0:
            return ToolAccessReport(
                minimum_axes=4,
                inaccessible_face_count=int(len(inaccessible_after_3)),
                inaccessible_face_indices=inaccessible_after_3[:100].tolist(),
                details=(
                    f"{len(inaccessible_after_3)} face(s) require 4-axis rotation"
                ),
            )

        return ToolAccessReport(
            minimum_axes=5,
            inaccessible_face_count=int(len(inaccessible_after_4)),
            inaccessible_face_indices=inaccessible_after_4[:100].tolist(),
            details=(f"{len(inaccessible_after_4)} face(s) require 5-axis machining"),
        )

    except Exception as exc:
        logger.warning("tool access analysis failed: %s", exc)
        return ToolAccessReport(
            minimum_axes=3,
            inaccessible_face_count=0,
            inaccessible_face_indices=[],
            details=f"Analysis failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Chatter risk (large flat overhangs in milling)
# ---------------------------------------------------------------------------


def detect_chatter_risk(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
    min_area_cm2: float = 4.0,
    max_thickness_mm: float = 3.0,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect large flat horizontal overhangs that risk vibration / chatter.

    These are planar faces with large area and thin cross-sections that
    vibrate when machined from above.

    Returns:
        (count, centroids, face_indices)
    """
    import trimesh

    count = 0
    centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 6:
            return 0, [], []

        face_normals = mesh.face_normals
        face_areas = mesh.area_faces
        face_centroids_arr = mesh.triangles_center

        # Find large horizontal faces (normal mostly vertical)
        horiz_mask = np.abs(face_normals[:, 2]) > 0.8
        large_mask = face_areas > (min_area_cm2 * 100.0)  # mm²

        candidate_mask = horiz_mask & large_mask
        candidate_indices = np.where(candidate_mask)[0]

        if len(candidate_indices) == 0:
            return 0, [], []

        # T3d: batch all ray casts in one call instead of one per face.
        # Each candidate face gets a downward ray from its centroid.
        ray_origins = face_centroids_arr[candidate_indices]  # (N, 3)
        ray_dirs = np.tile([0.0, 0.0, -1.0], (len(candidate_indices), 1))
        try:
            locs, ray_hit_idx, _ = mesh.ray.intersects_location(
                ray_origins, ray_dirs, multiple_hits=True
            )
            # Group hits by ray index, find max Z below each origin
            for i, fidx in enumerate(candidate_indices):
                c = face_centroids_arr[fidx]
                hit_mask = ray_hit_idx == i
                if not hit_mask.any():
                    continue
                hits = locs[hit_mask]
                hits_below = hits[hits[:, 2] < float(c[2]) - 0.1]
                if len(hits_below) == 0:
                    continue
                thickness = float(c[2]) - float(hits_below[:, 2].max())
                if thickness <= max_thickness_mm:
                    centroids.append([float(c[0]), float(c[1]), float(c[2])])
                    face_indices.append(int(fidx))
                    count += 1
        except Exception as _ray_err:
            logger.debug("chatter ray cast failed: %s", _ray_err)

    except Exception as exc:
        logger.warning("chatter risk detection failed: %s", exc)

    return count, centroids[:50], face_indices[:50]


# ---------------------------------------------------------------------------
# Tool deflection in deep narrow cavities
# ---------------------------------------------------------------------------


def detect_deep_narrow_cavities(
    cavities: list[CavityFeature],
) -> tuple[int, list[list[float]], list[int]]:
    """Flag cavities where depth > 4× width (tool deflection risk).

    Accepts the output of ``detect_cavities``.

    Returns:
        (count, centroids, face_indices)
    """
    count = 0
    centroids: list[list[float]] = []
    face_indices: list[int] = []

    threshold = MILLING_RULES.cavity_depth_ratio

    for cavity in cavities:
        if cavity.depth_ratio > threshold:
            centroids.append(cavity.centroid)
            face_indices.extend(cavity.face_indices)
            count += 1

    return count, centroids, face_indices


# ---------------------------------------------------------------------------
# T3e: Shared Z-slice profile — used by both detect_axial_symmetry and
# detect_grooves so both functions share one section_multiplane pass.
# ---------------------------------------------------------------------------


_AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}
_AXIS_VECTOR = {
    "X": [1.0, 0.0, 0.0],
    "Y": [0.0, 1.0, 0.0],
    "Z": [0.0, 0.0, 1.0],
}


def _compute_axis_slice_profile(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
    n_slices: int = 100,
    axis: str = "Z",
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Compute per-slice (axis value, mean_r, std_r, max_r) using section_multiplane.

    Returns four parallel lists: axis_values, mean_radii, std_radii, max_radii.
    Returns empty lists if mesh is invalid or slicing fails.
    """
    axis_values: list[float] = []
    mean_radii: list[float] = []
    std_radii: list[float] = []
    max_radii: list[float] = []

    try:
        axis_name = axis.upper()
        axis_index = _AXIS_INDEX.get(axis_name)
        if axis_index is None:
            return [], [], [], []

        bounds = mesh.bounds
        axis_min, axis_max = float(bounds[0, axis_index]), float(bounds[1, axis_index])
        total_length = axis_max - axis_min
        if total_length < 1.0:
            return [], [], [], []

        heights = np.linspace(
            axis_min + total_length / (2 * n_slices),
            axis_max - total_length / (2 * n_slices),
            n_slices,
        )
        plane_normal = np.zeros(3)
        plane_normal[axis_index] = 1.0
        plane_orig = np.array([0.0, 0.0, 0.0])

        sections = mesh.section_multiplane(
            plane_origin=plane_orig,
            plane_normal=plane_normal,
            heights=heights,
        )

        for axis_value, section in zip(heights, sections, strict=False):
            if section is None:
                continue
            try:
                if hasattr(section, "to_planar"):
                    path2d, _ = section.to_planar()
                else:
                    path2d = section

                pts = _extract_section_points(path2d)
                if pts is None or len(pts) < 4:
                    continue
                centroid = pts.mean(axis=0)
                radii = np.linalg.norm(pts - centroid, axis=1)
                mean_r = float(radii.mean())
                if mean_r < 1e-6:
                    continue
                axis_values.append(float(axis_value))
                mean_radii.append(mean_r)
                std_radii.append(float(radii.std()))
                max_radii.append(float(radii.max()))
            except Exception:
                continue

    except Exception as exc:
        logger.debug("_compute_axis_slice_profile failed for %s: %s", axis, exc)

    return axis_values, mean_radii, std_radii, max_radii


def _compute_z_slice_profile(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
    n_slices: int = 100,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Compute a legacy Z-axis turning profile."""
    return _compute_axis_slice_profile(mesh, n_slices=n_slices, axis="Z")


def _extract_section_loops(path2d: Any) -> list[npt.NDArray[np.float64]]:
    """Return closed or open 2D loops from a trimesh section path."""
    loops = getattr(path2d, "discrete", None)
    if loops:
        result: list[npt.NDArray[np.float64]] = []
        for loop in loops:
            pts = np.asarray(loop, dtype=float)
            if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
                pts = pts[:-1]
            if len(pts) >= 4:
                result.append(pts)
        return result

    vertices = getattr(path2d, "vertices", None)
    if vertices is None:
        return []

    pts = np.asarray(vertices, dtype=float)
    return [pts] if len(pts) >= 4 else []


def _extract_section_points(path2d: Any) -> npt.NDArray[np.float64] | None:
    """Return ordered 2D boundary points from a trimesh section path."""
    loops = _extract_section_loops(path2d)
    if loops:
        return max(loops, key=len)

    vertices = getattr(path2d, "vertices", None)
    if vertices is None:
        return None

    return np.asarray(vertices, dtype=float)


def _fit_circle_2d(
    points: npt.NDArray[np.float64],
) -> tuple[npt.NDArray[np.float64], float, float] | None:
    """Fit a 2D circle and return center, radius, and normalized deviation."""
    if len(points) < 8:
        return None

    pts = np.asarray(points[:, :2], dtype=float)
    x = pts[:, 0]
    y = pts[:, 1]
    if np.ptp(x) < 1e-6 or np.ptp(y) < 1e-6:
        return None

    try:
        matrix = np.column_stack((2.0 * x, 2.0 * y, np.ones_like(x)))
        rhs = x * x + y * y
        cx, cy, radius_term = np.linalg.lstsq(matrix, rhs, rcond=None)[0]
        radius_sq = float(radius_term + (cx * cx) + (cy * cy))
        if radius_sq <= 1e-9:
            return None

        center = np.array([float(cx), float(cy)], dtype=float)
        radius = math.sqrt(radius_sq)
        radii = np.linalg.norm(pts - center, axis=1)
        deviation = float(radii.std() / radius) if radius > 1e-9 else 1.0
        return center, radius, deviation
    except Exception:
        return None


def _round_axis_point(axis_point: npt.NDArray[np.float64]) -> list[float]:
    """Round axis coordinates for stable JSON/test output without hiding mm detail."""
    rounded: list[float] = []
    for value in axis_point:
        coordinate = round(float(value), 6)
        rounded.append(0.0 if abs(coordinate) < 1e-9 else coordinate)
    return rounded


def _compute_axis_point_from_circular_sections(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
    axis: str,
    n_slices: int = 24,
) -> list[float] | None:
    """Find a model-space point on the turning axis from circular section loops."""
    import trimesh

    axis_name = axis.upper()
    axis_index = _AXIS_INDEX.get(axis_name)
    if axis_index is None:
        return None

    try:
        bounds = np.asarray(mesh.bounds, dtype=float)
        axis_min = float(bounds[0, axis_index])
        axis_max = float(bounds[1, axis_index])
        total_length = axis_max - axis_min
        if total_length < 1.0:
            return None

        diagonal = float(np.linalg.norm(bounds[1] - bounds[0]))
        min_radius = max(diagonal * 0.002, 0.05)
        center_tolerance = max(diagonal * 0.015, 0.25)
        heights = np.linspace(
            axis_min + total_length * 0.08,
            axis_max - total_length * 0.08,
            max(8, n_slices),
        )
        plane_normal = np.zeros(3)
        plane_normal[axis_index] = 1.0

        samples: list[tuple[np.ndarray, float, float]] = []
        for axis_value in heights:
            plane_origin = np.zeros(3)
            plane_origin[axis_index] = float(axis_value)
            section = mesh.section(
                plane_origin=plane_origin,
                plane_normal=plane_normal,
            )
            if section is None or (
                not hasattr(section, "to_2D") and not hasattr(section, "to_planar")
            ):
                continue

            try:
                if hasattr(section, "to_2D"):
                    path2d, to_3d = section.to_2D()
                else:
                    path2d, to_3d = section.to_planar()
            except Exception:
                continue

            for loop in _extract_section_loops(path2d):
                fitted = _fit_circle_2d(loop)
                if fitted is None:
                    continue

                center_2d, radius, deviation = fitted
                if radius < min_radius or deviation > 0.08:
                    continue

                center_3d = trimesh.transformations.transform_points(
                    np.array([[center_2d[0], center_2d[1], 0.0]], dtype=float),
                    to_3d,
                )[0]
                samples.append((center_3d, radius, deviation))

        if not samples:
            return None

        groups: list[dict[str, Any]] = []
        perp_indices = [idx for idx in range(3) if idx != axis_index]
        for center_3d, radius, deviation in samples:
            matched = False
            for group in groups:
                group_center = group["sum"] / group["count"]
                if (
                    np.linalg.norm(center_3d[perp_indices] - group_center[perp_indices])
                    <= center_tolerance
                ):
                    group["sum"] += center_3d
                    group["count"] += 1
                    group["radius_sum"] += radius
                    group["deviation_sum"] += deviation
                    matched = True
                    break

            if not matched:
                groups.append(
                    {
                        "sum": center_3d.copy(),
                        "count": 1,
                        "radius_sum": radius,
                        "deviation_sum": deviation,
                    }
                )

        selected = max(
            groups,
            key=lambda group: (
                int(group["count"]),
                -float(group["deviation_sum"]) / int(group["count"]),
                -float(group["radius_sum"]) / int(group["count"]),
            ),
        )
        axis_point = selected["sum"] / selected["count"]
        axis_point[axis_index] = (axis_min + axis_max) / 2.0
        return _round_axis_point(axis_point)
    except Exception as exc:
        logger.debug("axis point detection failed for %s: %s", axis_name, exc)
        return None


# ---------------------------------------------------------------------------
# CNC Turning — axial symmetry detection
# ---------------------------------------------------------------------------


def detect_axial_symmetry(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
    n_slices: int = 20,
    symmetry_threshold: float = 0.15,
    slice_profile: tuple[list, list, list, list] | None = None,
) -> AxisSymmetryReport:
    """Determine if the part is a surface of revolution (suitable for turning).

    Strategy:
    1. Take cross-sections at ``n_slices`` heights along candidate axes (X/Y/Z).
    2. For each cross-section, fit a circle to the boundary contour.
    3. Measure deviation from circularity: std(radius) / mean(radius).
    4. If mean deviation < symmetry_threshold → turnable.

    Args:
        mesh: The mesh to analyse.
        n_slices: Number of slices to sample per candidate axis.
        symmetry_threshold: Max std/mean ratio for a turnable part.
        slice_profile: Optional pre-computed (z, mean_r, std_r, max_r) tuple
                       from _compute_z_slice_profile (T3e optimisation).

    Returns an AxisSymmetryReport.
    """
    import trimesh

    _FAIL = AxisSymmetryReport(  # noqa: N806
        is_turnable=False,
        primary_axis=None,
        axis_vector=[0.0, 0.0, 1.0],
        axis_point=None,
        length_diameter_ratio=None,
        symmetry_deviation=1.0,
    )

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 10:
            return _FAIL

        if slice_profile is not None:
            profile_candidates = [("Z", slice_profile)]
        else:
            profile_candidates = [
                (axis, _compute_axis_slice_profile(mesh, n_slices, axis))
                for axis in ("X", "Y", "Z")
            ]

        best_axis_name: str | None = None
        best_report: AxisSymmetryReport | None = None

        for axis_name, profile in profile_candidates:
            axis_vals, mean_rs, std_rs, _max_rs = profile
            if not axis_vals:
                continue

            axis_index = _AXIS_INDEX[axis_name]
            total_length = float(mesh.bounds[1, axis_index]) - float(
                mesh.bounds[0, axis_index]
            )
            if total_length < 1.0:
                continue

            # Sample every k-th slice when profile has more than n_slices entries
            step = max(1, len(axis_vals) // n_slices)
            deviations = [
                std_rs[i] / mean_rs[i]
                for i in range(0, len(axis_vals), step)
                if mean_rs[i] > 0
            ]
            radii_at_slices = [
                mean_rs[i] for i in range(0, len(axis_vals), step) if mean_rs[i] > 0
            ]

            if not deviations:
                continue

            mean_deviation = float(np.mean(deviations))
            mean_diameter = float(np.mean(radii_at_slices)) * 2.0
            ld_ratio = total_length / mean_diameter if mean_diameter > 0 else None
            candidate = AxisSymmetryReport(
                is_turnable=mean_deviation < symmetry_threshold,
                primary_axis=axis_name if mean_deviation < symmetry_threshold else None,
                axis_vector=_AXIS_VECTOR[axis_name],
                axis_point=None,
                length_diameter_ratio=ld_ratio,
                symmetry_deviation=mean_deviation,
            )
            if (
                best_report is None
                or candidate.symmetry_deviation < best_report.symmetry_deviation
            ):
                best_report = candidate
                best_axis_name = axis_name

        if best_report is None:
            return _FAIL

        if best_axis_name is not None:
            best_report.axis_point = _compute_axis_point_from_circular_sections(
                mesh,
                best_axis_name,
                n_slices=max(12, n_slices),
            )

        return best_report

    except Exception as exc:
        logger.warning("axial symmetry detection failed: %s", exc)
        return AxisSymmetryReport(
            is_turnable=False,
            primary_axis=None,
            axis_vector=[0.0, 0.0, 1.0],
            axis_point=None,
            length_diameter_ratio=None,
            symmetry_deviation=1.0,
        )


# ---------------------------------------------------------------------------
# Groove detection (for turning)
# ---------------------------------------------------------------------------


def detect_grooves(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
    n_slices: int = 100,
    slice_profile: tuple[list, list, list, list] | None = None,
    axis: str = "Z",
) -> list[GrooveFeature]:
    """Detect circumferential grooves on a turned part.

    Strategy:
    1. Take many cross-sections along the turning axis and measure the outer radius.
    2. A groove is a local dip in the radius profile (local minimum in radius).

    Args:
        mesh: The mesh to analyse.
        n_slices: Number of axis slices if computing from scratch.
        slice_profile: Optional pre-computed (axis, mean_r, std_r, max_r) profile.
        axis: Turning axis name ("X", "Y", or "Z").

    Returns a list of GrooveFeature objects.
    """
    import trimesh

    grooves: list[GrooveFeature] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 10:
            return grooves

        axis_name = axis.upper()
        axis_index = _AXIS_INDEX.get(axis_name, 2)
        if slice_profile is not None:
            axis_values, _mean_rs, _std_rs, outer_radii = slice_profile
        else:
            axis_values, _mean_rs, _std_rs, outer_radii = _compute_axis_slice_profile(
                mesh, n_slices, axis_name
            )

        if len(outer_radii) < 5:
            return grooves

        radii_arr = np.array(outer_radii)
        axis_arr = np.array(axis_values)
        mean_r = float(radii_arr.mean())

        # Find local minima (grooves = dips below mean)
        GROOVE_DEPTH_THRESH = mean_r * 0.05  # at least 5% dip  # noqa: N806

        in_groove = False
        groove_start = 0.0
        groove_min_r = mean_r

        for i in range(len(axis_arr)):
            r = float(radii_arr[i])
            if not in_groove and r < mean_r - GROOVE_DEPTH_THRESH:
                in_groove = True
                groove_start = float(axis_arr[i])
                groove_min_r = r
            elif in_groove:
                if r < groove_min_r:
                    groove_min_r = r
                if r >= mean_r - GROOVE_DEPTH_THRESH * 0.5 or i == len(axis_arr) - 1:
                    groove_end = float(axis_arr[i])
                    width = groove_end - groove_start
                    depth = mean_r - groove_min_r
                    centroid = [0.0, 0.0, 0.0]
                    centroid[axis_index] = (groove_start + groove_end) / 2.0
                    grooves.append(
                        GrooveFeature(
                            width_mm=width,
                            depth_mm=depth,
                            diameter_at_groove_mm=groove_min_r * 2.0,
                            centroid=centroid,
                            face_indices=[],
                        )
                    )
                    in_groove = False

    except Exception as exc:
        logger.warning("groove detection failed: %s", exc)

    return grooves[:50]


# ---------------------------------------------------------------------------
# Sharp corner analysis (existing, refactored with face indices)
# ---------------------------------------------------------------------------


def compute_sharp_corner_analysis(
    mesh: trimesh.Trimesh,  # type: ignore[name-defined]  # noqa: F821
    threshold_deg: float = 45.0,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect sharp internal corners unsuitable for standard CNC endmills.

    Finds concave edges where the interior angle between the two meeting faces
    is less than threshold_deg. These corners require EDM or leave a tool-
    radius fillet on the workpiece. Clusters nearby edges into distinct corner
    features to avoid counting tessellation facets as separate corners.

    Returns:
        (sharp_corner_count, centroids, face_indices)
    """
    import trimesh

    sharp_corner_count = 0
    sharp_corners: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 4:
            return 0, [], []

        # Build interior edge → [face0, face1] mapping
        edge_to_faces: dict[tuple[int, int], list[int]] = {}
        for fidx, face in enumerate(mesh.faces):
            for k in range(3):
                key = (
                    min(int(face[k]), int(face[(k + 1) % 3])),
                    max(int(face[k]), int(face[(k + 1) % 3])),
                )
                edge_to_faces.setdefault(key, []).append(fidx)

        face_normals = mesh.face_normals
        face_centroids = mesh.triangles_center
        vertices = mesh.vertices

        # For two faces meeting at a concave corner with interior angle α,
        # dot(n1, n2) = cos(α). Flag edges where α < threshold_deg.
        cos_thresh = math.cos(math.radians(threshold_deg))

        corner_mids: list[np.ndarray] = []
        corner_faces: list[tuple[int, int]] = []

        for (v0, v1), faces in edge_to_faces.items():
            if len(faces) != 2:
                continue
            fi, fj = faces[0], faces[1]
            ni, nj = face_normals[fi], face_normals[fj]
            ci, cj = face_centroids[fi], face_centroids[fj]

            # Concavity: both normals point toward each other across the edge.
            diff = cj - ci
            if not (
                float(np.dot(ni, diff)) < -0.05 and float(np.dot(nj, -diff)) < -0.05
            ):
                continue  # convex or boundary edge

            dot_n = float(np.clip(np.dot(ni, nj), -1.0, 1.0))
            if dot_n > cos_thresh:
                edge_mid = (vertices[v0] + vertices[v1]) / 2.0
                corner_mids.append(edge_mid)
                corner_faces.append((fi, fj))

        if not corner_mids:
            return 0, [], []

        # Cluster nearby corner edges so tessellation facets around the same
        # physical corner are counted as one feature, not hundreds.
        all_mids = np.array(corner_mids)
        cluster_radius = max(3.0, float(mesh.extents.max()) * 0.03)
        used: set[int] = set()

        for i in range(len(corner_mids)):
            if i in used:
                continue
            dists = np.linalg.norm(all_mids - corner_mids[i], axis=1)
            for idx in np.where(dists < cluster_radius)[0]:
                used.add(int(idx))

            mid = corner_mids[i]
            sharp_corners.append([float(mid[0]), float(mid[1]), float(mid[2])])
            fi, fj = corner_faces[i]
            face_indices.extend([fi, fj])
            sharp_corner_count += 1

    except Exception as exc:
        logger.warning("sharp corner analysis failed: %s", exc)

    return sharp_corner_count, sharp_corners[:100], list(set(face_indices))[:300]
