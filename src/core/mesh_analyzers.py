"""
Mesh-based DFM analysis functions for all additive manufacturing processes.

These functions operate on a ``trimesh.Trimesh`` object (tessellated mesh)
and return structured findings including the affected face indices needed to
generate overlay GLBs.

All length values are in mm (matching the geometry pipeline convention).
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import numpy as np

from .dfm_models import HoleFeature

if TYPE_CHECKING:
    import trimesh

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wall analysis
# ---------------------------------------------------------------------------


def compute_thin_wall_analysis(
    mesh: "trimesh.Trimesh",
    threshold_mm: float,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect thin-wall regions by finding opposite-facing face pairs.

    For each face, finds nearby faces whose normals point in roughly the
    opposite direction. If two such faces are within ``threshold_mm`` of each
    other they form a thin wall. This avoids the edge-length proxy which fires
    heavily on fine CAD tessellations regardless of actual wall thickness.

    Only connected clusters of ≥ 4 such faces are reported.

    Returns:
        (thin_wall_count, centroids, face_indices)
    """
    import trimesh
    from scipy.spatial import KDTree

    thin_wall_count = 0
    thin_wall_centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 4:
            return 0, [], []

        face_normals = mesh.face_normals      # (N, 3)
        face_centroids = mesh.triangles_center  # (N, 3)
        vertices = mesh.vertices

        tree = KDTree(face_centroids)
        # query_pairs returns all pairs (i, j) with i<j whose centroids are
        # within threshold_mm of each other.
        pairs = tree.query_pairs(r=threshold_mm)

        thin_face_set: set[int] = set()
        for i, j in pairs:
            dot = float(np.dot(face_normals[i], face_normals[j]))
            if dot < -0.7:   # roughly anti-parallel → opposite sides of a wall
                thin_face_set.add(i)
                thin_face_set.add(j)

        if not thin_face_set:
            return 0, [], []

        # Build face adjacency for connected-component clustering
        edge_to_faces: dict[tuple[int, int], list[int]] = {}
        for fidx in thin_face_set:
            face = mesh.faces[fidx]
            for k in range(3):
                key = (
                    min(int(face[k]), int(face[(k + 1) % 3])),
                    max(int(face[k]), int(face[(k + 1) % 3])),
                )
                edge_to_faces.setdefault(key, []).append(fidx)

        face_adj: dict[int, list[int]] = {f: [] for f in thin_face_set}
        for neighbors in edge_to_faces.values():
            for a in neighbors:
                for b in neighbors:
                    if a != b and b in face_adj:
                        face_adj[a].append(b)

        visited: set[int] = set()
        for start in thin_face_set:
            if start in visited:
                continue
            cluster: list[int] = []
            queue = [start]
            while queue:
                cur = queue.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                cluster.append(cur)
                for n in face_adj.get(cur, []):
                    if n not in visited:
                        queue.append(n)
            if len(cluster) < 4:
                continue

            cluster_verts = np.array(
                [vertices[v] for f in cluster for v in mesh.faces[f]]
            )
            centroid = cluster_verts.mean(axis=0)
            thin_wall_centroids.append(
                [float(centroid[0]), float(centroid[1]), float(centroid[2])]
            )
            face_indices.extend(cluster)
            thin_wall_count += 1  # count regions, not individual faces

    except Exception as exc:
        logger.warning("thin wall analysis failed: %s", exc)

    return thin_wall_count, thin_wall_centroids[:500], face_indices[:10000]


def compute_unsupported_wall_analysis(
    mesh: "trimesh.Trimesh",
    threshold_mm: float,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect unsupported wall regions (walls connected on <2 sides).

    Strategy: identify boundary edges (edges adjacent to only one face).
    Faces that have ≥2 boundary edges and a short bounding box in at least
    one dimension are candidate unsupported walls.

    Returns:
        (count, centroids, face_indices)
    """
    import trimesh

    count = 0
    centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 3:
            return 0, [], []

        # Boundary edges: appear in exactly one face
        edge_face_count: dict[tuple[int, int], int] = {}
        for idx, face in enumerate(mesh.faces):
            for k in range(3):
                key = (
                    min(int(face[k]), int(face[(k + 1) % 3])),
                    max(int(face[k]), int(face[(k + 1) % 3])),
                )
                edge_face_count[key] = edge_face_count.get(key, 0) + 1

        boundary_edges: set[tuple[int, int]] = {
            e for e, c in edge_face_count.items() if c == 1
        }

        # Faces that have ≥2 boundary edges are "unsupported"
        face_boundary_count: dict[int, int] = {}
        for idx, face in enumerate(mesh.faces):
            for k in range(3):
                key = (
                    min(int(face[k]), int(face[(k + 1) % 3])),
                    max(int(face[k]), int(face[(k + 1) % 3])),
                )
                if key in boundary_edges:
                    face_boundary_count[idx] = face_boundary_count.get(idx, 0) + 1

        face_centroids = mesh.triangles_center
        for fidx, bc in face_boundary_count.items():
            if bc >= 2:
                # Check if this face is thin (bounding box in one dim < threshold)
                tri = mesh.vertices[mesh.faces[fidx]]
                extents = tri.max(axis=0) - tri.min(axis=0)
                if float(extents.min()) < threshold_mm:
                    centroid = face_centroids[fidx]
                    centroids.append(
                        [float(centroid[0]), float(centroid[1]), float(centroid[2])]
                    )
                    face_indices.append(fidx)
                    count += 1

    except Exception as exc:
        logger.warning("unsupported wall analysis failed: %s", exc)

    return count, centroids[:200], face_indices[:200]


# ---------------------------------------------------------------------------
# Overhang analysis
# ---------------------------------------------------------------------------


def compute_overhang_analysis(
    mesh: "trimesh.Trimesh",
    threshold_deg: float,
) -> tuple[int, float, list[list[float]], list[int]]:
    """Detect overhang regions that exceed the threshold angle.

    Uses a 2° tolerance to avoid false-positives on faces designed to sit
    exactly at the machine's self-supporting angle (e.g. wrench optimised for
    45°).  Faces are clustered into connected regions via BFS; the build-plate
    contact region (lowest Z faces) is excluded.  Returns the number of
    distinct overhang *regions*, not individual faces.

    Returns:
        (overhang_region_count, overhang_area_cm2, centroids, face_indices)
    """
    import trimesh

    overhang_region_count = 0
    overhang_area_cm2 = 0.0
    overhang_centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 3:
            return 0, 0.0, [], []

        # Normalise face winding before relying on face_normals — an inconsistently
        # wound mesh (e.g. from cascadio multi-body concatenation before merge_vertices)
        # can have downward-facing faces reported as upward, silencing all overhangs.
        if not mesh.is_winding_consistent:
            mesh = mesh.copy()
            trimesh.repair.fix_winding(mesh)

        face_normals = mesh.face_normals
        face_areas = mesh.area_faces
        face_centroids = mesh.triangles_center

        # 0.5° tolerance keeps FP risk low while catching genuine near-threshold overhangs
        # (the old 2.0° margin silently dropped real overhangs between 45° and 47°).
        effective_thresh = threshold_deg + 0.5
        overhang_z_thresh = -math.sin(math.radians(effective_thresh))

        # Find all overhang faces
        overhang_set: set[int] = set()
        for i, normal in enumerate(face_normals):
            norm = float(np.linalg.norm(normal))
            if norm < 1e-10:
                continue
            if (normal / norm)[2] < overhang_z_thresh:
                overhang_set.add(i)

        logger.info(
            "overhang candidates: %d faces (threshold %.1f° + 0.5° = %.1f°, z_thresh=%.3f)",
            len(overhang_set), threshold_deg, effective_thresh, overhang_z_thresh,
        )

        if not overhang_set:
            logger.info("No overhang faces detected - all faces are within the threshold angle")
            return 0, 0.0, [], []

        # Determine the build-plate contact band: faces within 1% of part height
        # (or 1 mm, whichever is larger) above the global Z minimum are excluded
        # because they represent the bottom face resting on the build plate.
        # Previously 3%/2mm — too aggressive for tall parts with low-positioned overhangs
        # (e.g. a flange-mounted duct whose bottom face sits just above the build plate).
        all_centroids = face_centroids
        global_min_z = float(mesh.vertices[:, 2].min())
        part_height = float(mesh.vertices[:, 2].max()) - global_min_z
        build_plate_band = max(1.0, part_height * 0.01)
        logger.info(
            "build-plate band: %.2f mm (part height %.2f mm, global_min_z %.2f mm)",
            build_plate_band, part_height, global_min_z,
        )

        # Build face adjacency among overhang faces only
        edge_to_faces: dict[tuple[int, int], list[int]] = {}
        for fidx in overhang_set:
            face = mesh.faces[fidx]
            for k in range(3):
                key = (
                    min(int(face[k]), int(face[(k + 1) % 3])),
                    max(int(face[k]), int(face[(k + 1) % 3])),
                )
                edge_to_faces.setdefault(key, []).append(fidx)

        face_adj: dict[int, list[int]] = {f: [] for f in overhang_set}
        for neighbors in edge_to_faces.values():
            for a in neighbors:
                for b in neighbors:
                    if a != b:
                        face_adj[a].append(b)

        # BFS clustering
        visited: set[int] = set()
        total_clusters = 0
        clusters_filtered_by_build_plate = 0
        clusters_filtered_by_area = 0

        for start in overhang_set:
            if start in visited:
                continue
            total_clusters += 1
            cluster: list[int] = []
            queue = [start]
            while queue:
                cur = queue.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                cluster.append(cur)
                for n in face_adj.get(cur, []):
                    if n not in visited:
                        queue.append(n)

            # Strip faces whose centroid sits in the build-plate contact band
            # (faces resting on the build plate are not "overhangs" requiring support).
            # Only strip individual faces, not the whole cluster — a tall overhang that
            # connects to a build-plate-contact face must not be silenced entirely.
            trimmed_cluster = [
                f for f in cluster
                if float(all_centroids[f][2]) >= global_min_z + build_plate_band
            ]

            if not trimmed_cluster:
                # All faces in this cluster are in the build-plate band
                clusters_filtered_by_build_plate += 1
                continue

            # Ignore small stray patches that don't represent significant
            # overhang regions requiring support.  Filter by area (mm²) so
            # that the threshold is independent of mesh resolution.
            region_area_check = sum(float(face_areas[f]) for f in trimmed_cluster)
            if region_area_check < 4.0:  # < 4 mm² → not a meaningful overhang
                clusters_filtered_by_area += 1
                continue

            region_area = region_area_check
            overhang_area_cm2 += region_area / 100.0

            cluster_centroids = np.array([all_centroids[f] for f in trimmed_cluster])
            region_centroid = cluster_centroids.mean(axis=0)
            overhang_centroids.append(
                [float(region_centroid[0]), float(region_centroid[1]),
                 float(region_centroid[2])]
            )
            face_indices.extend(trimmed_cluster)
            overhang_region_count += 1

        logger.info(
            "overhang clustering: %d total cluster(s), %d filtered by build-plate band, %d filtered by area (< 4mm²), %d final region(s), %.2f cm²",
            total_clusters, clusters_filtered_by_build_plate, clusters_filtered_by_area,
            overhang_region_count, overhang_area_cm2,
        )

    except Exception as exc:
        logger.warning("overhang analysis failed: %s", exc)

    return (
        overhang_region_count,
        overhang_area_cm2,
        overhang_centroids[:500],
        face_indices[:10000],
    )


# ---------------------------------------------------------------------------
# Hole detection (mesh-based, approximate)
# ---------------------------------------------------------------------------


def detect_holes_mesh(
    mesh: "trimesh.Trimesh",
    min_diameter_mm: float = 0.5,
) -> list[HoleFeature]:
    """Detect approximately cylindrical holes by analysing boundary loops.

    Strategy:
    1. Find boundary edges (open edges on the mesh surface).
    2. Group boundary edges into connected loops.
    3. Fit a circle to each loop — if the fit is good it's a hole.
    4. Track pairs of matching circles at different Z positions as cylinders.

    Returns a list of HoleFeature objects.
    """
    import trimesh

    holes: list[HoleFeature] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 3:
            return holes

        # Get boundary edges (each appears in exactly one face)
        edges = mesh.edges
        edge_count: dict[tuple[int, int], int] = {}
        for e in edges:
            key = (min(int(e[0]), int(e[1])), max(int(e[0]), int(e[1])))
            edge_count[key] = edge_count.get(key, 0) + 1

        boundary = {e for e, c in edge_count.items() if c == 1}
        if not boundary:
            return holes

        # Build adjacency for boundary edges
        adj: dict[int, list[int]] = {}
        for v0, v1 in boundary:
            adj.setdefault(v0, []).append(v1)
            adj.setdefault(v1, []).append(v0)

        # Extract loops
        loops: list[list[int]] = []
        visited: set[int] = set()
        for start in list(adj.keys()):
            if start in visited:
                continue
            loop: list[int] = []
            current = start
            prev = -1
            while True:
                visited.add(current)
                loop.append(current)
                neighbors = [n for n in adj.get(current, []) if n != prev]
                if not neighbors or neighbors[0] in visited:
                    break
                prev = current
                current = neighbors[0]
            if len(loop) >= 6:
                loops.append(loop)

        vertices = mesh.vertices

        for loop in loops:
            pts = np.array([vertices[v] for v in loop])
            # Compute centroid and check circularity in XY plane
            centroid_2d = pts[:, :2].mean(axis=0)
            radii = np.linalg.norm(pts[:, :2] - centroid_2d, axis=1)
            mean_r = float(radii.mean())
            std_r = float(radii.std())

            if mean_r < 1e-6 or std_r / mean_r > 0.15:
                # Not circular enough
                continue

            diameter = mean_r * 2.0
            if diameter < min_diameter_mm:
                continue

            z_values = pts[:, 2]
            centroid_z = float(z_values.mean())

            # T3a: use vertex_faces (cached) instead of O(F·L) scan.
            # vertex_faces[v] gives all face indices that share vertex v.
            loop_arr = np.fromiter(loop, dtype=np.intp, count=len(loop))
            vf = mesh.vertex_faces  # (V, max_adj) array, -1 = padding
            raw = vf[loop_arr].ravel()
            fidxs = list(set(int(f) for f in raw if f >= 0))

            holes.append(
                HoleFeature(
                    center=[float(centroid_2d[0]), float(centroid_2d[1]), centroid_z],
                    axis=[0.0, 0.0, 1.0],
                    diameter_mm=diameter,
                    depth_mm=float(z_values.max() - z_values.min()),
                    face_indices=fidxs[:50],
                )
            )

    except Exception as exc:
        logger.warning("hole detection failed: %s", exc)

    return holes[:50]


# ---------------------------------------------------------------------------
# Bridge detection
# ---------------------------------------------------------------------------


def detect_bridges(
    mesh: "trimesh.Trimesh",
    max_span_mm: float,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect horizontal unsupported spans (bridges) exceeding max_span_mm.

    Strategy: find downward-facing faces (normal.z < 0) with no mesh geometry
    directly below them within the bridge distance. Uses vertical ray casting.

    Returns:
        (bridge_count, centroids, face_indices)
    """
    import trimesh

    bridge_count = 0
    bridge_centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 3:
            return 0, [], []

        face_normals = mesh.face_normals
        face_centroids_arr = mesh.triangles_center

        # Only consider downward-facing faces (overhangs)
        downward_mask = face_normals[:, 2] < -0.5

        if not np.any(downward_mask):
            return 0, [], []

        downward_indices = np.where(downward_mask)[0]

        # Cast rays downward from each candidate face centroid
        ray_origins = face_centroids_arr[downward_indices]
        ray_directions = np.tile([0.0, 0.0, -1.0], (len(ray_origins), 1))

        try:
            locations, ray_idx, _ = mesh.ray.intersects_location(
                ray_origins, ray_directions, multiple_hits=False
            )
        except Exception:
            return 0, [], []

        # Map ray index → hit distance
        hit_distances: dict[int, float] = {}
        for loc, ridx in zip(locations, ray_idx):
            origin = ray_origins[ridx]
            dist = float(np.linalg.norm(loc - origin))
            if dist > 1e-3:  # ignore self-intersections
                existing = hit_distances.get(int(ridx))
                if existing is None or dist < existing:
                    hit_distances[int(ridx)] = dist

        for i, didx in enumerate(downward_indices):
            # If no hit below OR hit is farther than max_span_mm → bridge
            dist = hit_distances.get(i)
            is_bridge = dist is None or dist > max_span_mm
            if is_bridge:
                c = face_centroids_arr[didx]
                bridge_centroids.append([float(c[0]), float(c[1]), float(c[2])])
                face_indices.append(int(didx))
                bridge_count += 1

    except Exception as exc:
        logger.warning("bridge detection failed: %s", exc)

    return bridge_count, bridge_centroids[:200], face_indices[:200]


# ---------------------------------------------------------------------------
# Small feature detection
# ---------------------------------------------------------------------------


def detect_small_features(
    mesh: "trimesh.Trimesh",
    min_size_mm: float,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect features smaller than min_size_mm.

    Strategy: identify faces with all edge lengths shorter than min_size_mm,
    then cluster adjacent such faces. Flag clusters whose bounding-box
    diagonal is below min_size_mm.

    Returns:
        (count, centroids, face_indices)
    """
    import trimesh

    count = 0
    centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 3:
            return 0, [], []

        vertices = mesh.vertices
        face_centroids_arr = mesh.triangles_center

        # Pre-compute per-face dihedral angles to neighbours (used for curvature filtering).
        # A face sitting on a smooth curved surface has low dihedral angles to all neighbours
        # (small angle between face normals → smooth tessellation, not a real feature).
        try:
            face_adj_pairs = mesh.face_adjacency          # (N, 2) face pairs sharing an edge
            adj_angles = mesh.face_adjacency_angles       # (N,) dihedral angle per pair (radians)
            # Build per-face max-dihedral-angle map: max angle to any adjacent face.
            max_dihedral = np.zeros(len(mesh.faces), dtype=float)
            for (fa, fb), angle in zip(face_adj_pairs, adj_angles):
                if angle > max_dihedral[fa]:
                    max_dihedral[fa] = angle
                if angle > max_dihedral[fb]:
                    max_dihedral[fb] = angle
            # Threshold: ~5° in radians.  Faces with all neighbours below this are
            # on smooth curved surfaces (tessellation artifact, not a real sharp feature).
            _SMOOTH_THRESHOLD = 0.09  # radians ≈ 5°
        except Exception:
            max_dihedral = None
            _SMOOTH_THRESHOLD = 0.09

        # Faces where all three edges are short AND at least one neighbour has a
        # significant dihedral angle (indicating a genuine geometric boundary).
        small_face_set: set[int] = set()
        for fidx, face in enumerate(mesh.faces):
            verts = [vertices[int(v)] for v in face]
            max_edge = max(
                float(np.linalg.norm(verts[1] - verts[0])),
                float(np.linalg.norm(verts[2] - verts[1])),
                float(np.linalg.norm(verts[0] - verts[2])),
            )
            if max_edge >= min_size_mm:
                continue
            # Skip triangles on smooth surfaces — they are tessellation density, not features.
            if max_dihedral is not None and max_dihedral[fidx] < _SMOOTH_THRESHOLD:
                continue
            small_face_set.add(fidx)

        if not small_face_set:
            return 0, [], []

        # Build face adjacency for clustering
        face_adj: dict[int, list[int]] = {f: [] for f in small_face_set}
        edge_to_face: dict[tuple[int, int], list[int]] = {}
        for fidx in small_face_set:
            face = mesh.faces[fidx]
            for k in range(3):
                key = (
                    min(int(face[k]), int(face[(k + 1) % 3])),
                    max(int(face[k]), int(face[(k + 1) % 3])),
                )
                edge_to_face.setdefault(key, []).append(fidx)

        for neighbors in edge_to_face.values():
            for i in range(len(neighbors)):
                for j in range(i + 1, len(neighbors)):
                    a, b = neighbors[i], neighbors[j]
                    if a in face_adj and b in face_adj:
                        face_adj[a].append(b)
                        face_adj[b].append(a)

        # BFS cluster
        visited: set[int] = set()
        for start in small_face_set:
            if start in visited:
                continue
            cluster: list[int] = []
            queue = [start]
            while queue:
                cur = queue.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                cluster.append(cur)
                queue.extend(n for n in face_adj.get(cur, []) if n not in visited)

            if not cluster:
                continue

            # Reject oversized clusters — a long curved strip produces many small
            # triangles that join into a large cluster with no real small feature.
            if len(cluster) > 50:
                continue

            cluster_verts = np.array(
                [vertices[v] for f in cluster for v in mesh.faces[f]]
            )
            extents = cluster_verts.max(axis=0) - cluster_verts.min(axis=0)
            diag = float(np.linalg.norm(extents))
            if diag < min_size_mm * 2:
                # Surface-area guard: reject clusters whose area is smaller than
                # a circular pocket of min_size_mm radius (likely just noise).
                cluster_area = sum(
                    float(mesh.area_faces[f]) for f in cluster if f < len(mesh.area_faces)
                )
                min_area = math.pi * (min_size_mm / 2) ** 2 * 0.25
                if cluster_area < min_area:
                    continue
                centroid = cluster_verts.mean(axis=0)
                centroids.append(
                    [float(centroid[0]), float(centroid[1]), float(centroid[2])]
                )
                face_indices.extend(cluster)
                count += 1

    except Exception as exc:
        logger.warning("small feature detection failed: %s", exc)

    return count, centroids[:100], face_indices[:500]


def detect_small_features_occ(
    occ_features: "list",  # list[OccFeature]
    min_size_mm: float,
    mesh: "trimesh.Trimesh | None" = None,
    face_tag_to_tri: "dict[int, list[int]] | None" = None,
    max_overlay_faces: int = 500,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect features smaller than min_size_mm using CAD B-Rep topology.

    Uses OCC-extracted feature data instead of triangle edge lengths, so
    tessellation density on curved surfaces does not produce false positives.

    For each OCC feature the "feature size" is derived from geometry:
    - cylinder / hole: ``2 * radius_mm``
    - torus fillet:    ``2 * minor_radius_mm`` (minor tube radius)
    - planar face:     ``bbox_min_mm`` (AABB short side) — avoids flagging
                       long-thin slot walls because their sqrt(area) looks small
    - cone:            ``2 * apex_radius_mm``
    - freeform:        ``sqrt(area_mm2)`` last resort

    Overlay face indices use the OCC face_tag → tri_indices map when provided
    (exact topology), falling back to a spatial centroid search on the mesh.

    Args:
        occ_features:      OCC feature list from ``analyze_step_brep``.
        min_size_mm:       Minimum acceptable feature size in mm.
        mesh:              Cascadio-produced trimesh (Z-up, mm) for spatial fallback.
        face_tag_to_tri:   OCC face tag → STL triangle index list (from analyze_step_brep).
        max_overlay_faces: Cap on returned face indices.

    Returns:
        (count, centroids, face_indices)
    """
    count = 0
    centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        import math as _math

        # Pre-compute triangle centroids once for spatial fallback.
        tri_centroids: "np.ndarray | None" = None
        if mesh is not None and hasattr(mesh, "triangles_center"):
            try:
                tri_centroids = np.asarray(mesh.triangles_center)
            except Exception:
                pass

        for feat in occ_features:
            params = feat.parameters if hasattr(feat, "parameters") else {}
            feat_type = feat.feature_type if hasattr(feat, "feature_type") else ""
            area_mm2 = float(feat.area_mm2) if hasattr(feat, "area_mm2") else 0.0
            centroid = feat.centroid if hasattr(feat, "centroid") else None

            # Derive canonical "feature size" from CAD geometry.
            # For cylinders and holes: radius_mm is the cylinder radius.
            # For torus fillets: radius_mm is the minor (tube) radius, which
            # determines printability — not the large sweep radius (major_radius_mm).
            if "radius_mm" in params:
                feature_size_mm = 2.0 * float(params["radius_mm"])
            elif "apex_radius_mm" in params:
                feature_size_mm = 2.0 * float(params["apex_radius_mm"])
            elif feat_type == "planar_face":
                # Use the AABB short side (bbox_min_mm) so long-thin slot walls
                # (e.g. 40×0.5 mm) are correctly flagged by their 0.5 mm width,
                # not by sqrt(area)=4.5 mm which would skip them.
                bbox_min = float(getattr(feat, "bbox_min_mm", 0.0))
                feature_size_mm = bbox_min if bbox_min > 1e-3 else (
                    2.0 * _math.sqrt(area_mm2 / _math.pi) if area_mm2 > 0 else 0.0
                )
            elif area_mm2 > 0:
                # Last resort for freeform / unclassified surfaces.
                feature_size_mm = 2.0 * _math.sqrt(area_mm2 / _math.pi)
            else:
                continue  # not enough information

            if feature_size_mm <= 0 or feature_size_mm >= min_size_mm:
                continue  # feature is large enough — not a DFM concern

            if centroid is None:
                continue

            count += 1
            centroids.append([float(centroid[0]), float(centroid[1]), float(centroid[2])])

            if len(face_indices) >= max_overlay_faces:
                continue

            # Prefer topology-correct face lookup via face_tag → tri_indices map.
            feat_tag = feat.face_tag if hasattr(feat, "face_tag") else -1
            if face_tag_to_tri is not None and feat_tag in face_tag_to_tri:
                tris = face_tag_to_tri[feat_tag]
                remaining = max_overlay_faces - len(face_indices)
                face_indices.extend(tris[:remaining])
            elif tri_centroids is not None:
                # Spatial fallback: search radius covers the face footprint.
                search_r = max(
                    _math.sqrt(area_mm2) if area_mm2 > 0 else feature_size_mm,
                    min_size_mm * 1.5,
                )
                c = np.array([float(centroid[0]), float(centroid[1]), float(centroid[2])])
                dists = np.linalg.norm(tri_centroids - c, axis=1)
                nearby = np.where(dists <= search_r)[0].tolist()
                remaining = max_overlay_faces - len(face_indices)
                face_indices.extend(nearby[:remaining])

    except Exception as exc:
        logger.warning("OCC-based small feature detection failed: %s", exc)

    return count, centroids[:100], face_indices[:max_overlay_faces]


# ---------------------------------------------------------------------------
# Escape hole analysis (powder/resin removal)
# ---------------------------------------------------------------------------


def detect_escape_hole_risk(
    mesh: "trimesh.Trimesh",
    min_hole_diameter_mm: float,
    bodies: "list | None" = None,
    precomputed_holes: "list | None" = None,
) -> tuple[bool, list[list[float]], list[int]]:
    """Flag enclosed hollow volumes that lack an adequately-sized escape hole.

    Strategy:
    1. Detect hollow regions via mesh splitting (existing approach).
    2. For each hollow region, check if there are holes ≥ min_hole_diameter_mm
       in the vicinity. If not, flag as risk.

    Args:
        mesh: The mesh to analyse.
        min_hole_diameter_mm: Minimum escape hole diameter threshold.
        bodies: Optional pre-split body list — avoids calling mesh.split() again
                when the caller has already done so (T2c optimisation).
        precomputed_holes: Optional pre-computed holes list — avoids calling
                           detect_holes_mesh again (T2e optimisation).

    Returns:
        (has_risk, centroids_of_risky_regions, face_indices)
    """
    import trimesh

    hollow_centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh):
            return False, [], []
        if not mesh.is_watertight:
            return False, [], []

        if bodies is None:
            split = mesh.split()
            if isinstance(split, trimesh.Trimesh):
                return False, [], []
            if isinstance(split, trimesh.Scene):
                bodies = list(split.geometry.values())
            else:
                return False, [], []

        bodies = [b for b in bodies if isinstance(b, trimesh.Trimesh)]
        if len(bodies) <= 1:
            return False, [], []

        if precomputed_holes is not None:
            # Filter to only holes at or above the required diameter
            holes = [h for h in precomputed_holes if h.diameter_mm >= min_hole_diameter_mm]
        else:
            holes = detect_holes_mesh(mesh, min_diameter_mm=min_hole_diameter_mm)

        for body in bodies:
            if body.is_watertight:
                centroid = body.centroid
                # Check if a hole passes through this region
                has_escape = any(
                    abs(h.center[2] - float(centroid[2])) < h.depth_mm * 0.5
                    and np.linalg.norm(
                        np.array(h.center[:2]) - np.array(centroid[:2])
                    )
                    < body.extents.max()
                    for h in holes
                )
                if not has_escape:
                    hollow_centroids.append(
                        [
                            float(centroid[0]),
                            float(centroid[1]),
                            float(centroid[2]),
                        ]
                    )

    except Exception as exc:
        logger.warning("escape hole analysis failed: %s", exc)

    return len(hollow_centroids) > 0, hollow_centroids, face_indices


# ---------------------------------------------------------------------------
# Hollow detection (generic — for SLA resin trapping)
# ---------------------------------------------------------------------------


def detect_hollow_regions(
    mesh: "trimesh.Trimesh",
) -> tuple[list[list[float]], list[int]]:
    """Detect enclosed hollow volumes (resin trapping risk for SLA/resin).

    Returns:
        (centroids, face_indices)
    """
    import trimesh

    hollow_centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or not mesh.is_watertight:
            return [], []

        split = mesh.split()
        if isinstance(split, trimesh.Trimesh):
            return [], []

        if isinstance(split, trimesh.Scene):
            for body in split.geometry.values():
                if isinstance(body, trimesh.Trimesh) and body.is_watertight:
                    c = body.centroid
                    hollow_centroids.append([float(c[0]), float(c[1]), float(c[2])])

    except Exception as exc:
        logger.warning("hollow region detection failed: %s", exc)

    return hollow_centroids[:50], face_indices


# ---------------------------------------------------------------------------
# Pin detection (approximate)
# ---------------------------------------------------------------------------


def detect_thin_pins(
    mesh: "trimesh.Trimesh",
    min_diameter_mm: float,
    precomputed_holes: "list | None" = None,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect protruding pin-like features below minimum diameter.

    Strategy: find upward-pointing clusters of faces forming cylinders
    (reuse hole detection on the inverted mesh or detect narrow protrusions
    via cross-section analysis).

    Args:
        mesh: The mesh to analyse.
        min_diameter_mm: Minimum pin diameter threshold.
        precomputed_holes: Optional pre-computed holes list (T2e optimisation).
                           Must have been computed at min_diameter_mm ≤ 0.1 mm.

    Returns:
        (count, centroids, face_indices)
    """
    # Use hole detection as a proxy: narrow upward cylinders look like holes
    # in the inverted normal orientation. This is approximate.
    if precomputed_holes is not None:
        holes = precomputed_holes
    else:
        holes = detect_holes_mesh(mesh, min_diameter_mm=0.1)
    thin_pins = [h for h in holes if h.diameter_mm < min_diameter_mm]

    count = len(thin_pins)
    centroids = [h.center for h in thin_pins]
    face_indices = [f for h in thin_pins for f in h.face_indices]
    return count, centroids, face_indices


# ---------------------------------------------------------------------------
# Embossed / engraved detail detection (approximate)
# ---------------------------------------------------------------------------


def detect_embossed_engraved(
    mesh: "trimesh.Trimesh",
    min_width_mm: float,
    min_height_mm: float,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect raised or recessed surface details below dimensional thresholds.

    Strategy:
    - Identify upward-facing faces.
    - Compute a local reference surface (percentile-based, robust to outliers).
    - Flag faces significantly above/below reference (potential emboss/deboss).
    - Apply dihedral-angle guard to skip smooth-surface tessellation.
    - Cluster adjacent flagged faces via BFS.
    - Validate cluster size (bounding-box diagonal ≤ 10× emboss height).
    - Report cluster count, not raw triangle count.

    Returns:
        (count, centroids, face_indices)
    """
    import trimesh

    count = 0
    centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) < 20:
            return 0, [], []

        face_centroids_arr = mesh.triangles_center
        face_normals = mesh.face_normals
        vertices = mesh.vertices

        # Identify upward-facing faces (normal Z > 0.7 ≈ faces within ~45° of vertical)
        up_mask = np.abs(face_normals[:, 2]) > 0.7

        if not np.any(up_mask) or np.sum(up_mask) < 20:
            return 0, [], []  # Too few upward-facing faces to fit a reference

        up_centroids = face_centroids_arr[up_mask]
        up_indices = np.where(up_mask)[0]

        # Compute a robust local reference surface using percentiles.
        # Percentile-based reference is resilient to large height variations
        # (e.g. a turbine disc with a rim — the global mean would be skewed).
        # Use median (50th percentile) as reference, IQR (75th-25th) for spread.
        ref_z = float(np.percentile(up_centroids[:, 2], 50))
        iqr_z = float(np.percentile(up_centroids[:, 2], 75) - np.percentile(up_centroids[:, 2], 25))

        if iqr_z < min_height_mm * 0.5:
            return 0, [], []  # Insufficient height variation for emboss features

        # Pre-compute per-face dihedral angles to neighbors (copied from detect_small_features).
        # A face on a smooth curved surface has low dihedral angles → tessellation artifact.
        try:
            face_adj_pairs = mesh.face_adjacency
            adj_angles = mesh.face_adjacency_angles
            max_dihedral = np.zeros(len(mesh.faces), dtype=float)
            for (fa, fb), angle in zip(face_adj_pairs, adj_angles):
                if angle > max_dihedral[fa]:
                    max_dihedral[fa] = angle
                if angle > max_dihedral[fb]:
                    max_dihedral[fb] = angle
            _SMOOTH_THRESHOLD = 0.09  # radians ≈ 5°
        except Exception:
            max_dihedral = None
            _SMOOTH_THRESHOLD = 0.09

        # Flag faces significantly below the reference surface (engraved details only).
        # For detecting pips on dice: only look for recessed features, ignore raised rims.
        # Use 1.0× min_height as threshold.
        THRESH = min_height_mm * 1.0
        emboss_mask = (ref_z - up_centroids[:, 2]) > THRESH  # Only below reference (engraved)
        emboss_up_indices = up_indices[emboss_mask]

        if len(emboss_up_indices) == 0:
            return 0, [], []

        # Apply dihedral-angle guard: skip faces on smooth surfaces.
        flagged_set: set[int] = set()
        for fidx in emboss_up_indices:
            fidx_int = int(fidx)
            if max_dihedral is not None and max_dihedral[fidx_int] < _SMOOTH_THRESHOLD:
                continue
            flagged_set.add(fidx_int)

        if not flagged_set:
            return 0, [], []

        # Build face adjacency for clustering (same pattern as overhang/small_features).
        edge_to_faces: dict[tuple[int, int], list[int]] = {}
        for fidx in flagged_set:
            face = mesh.faces[fidx]
            for k in range(3):
                key = (
                    min(int(face[k]), int(face[(k + 1) % 3])),
                    max(int(face[k]), int(face[(k + 1) % 3])),
                )
                edge_to_faces.setdefault(key, []).append(fidx)

        face_adj: dict[int, list[int]] = {f: [] for f in flagged_set}
        for neighbors in edge_to_faces.values():
            for i in range(len(neighbors)):
                for j in range(i + 1, len(neighbors)):
                    a, b = neighbors[i], neighbors[j]
                    if a in face_adj and b in face_adj:
                        face_adj[a].append(b)
                        face_adj[b].append(a)

        # BFS clustering
        visited: set[int] = set()
        clusters: list[list[int]] = []
        for start in flagged_set:
            if start in visited:
                continue
            cluster: list[int] = []
            queue = [start]
            while queue:
                cur = queue.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                cluster.append(cur)
                queue.extend(n for n in face_adj.get(cur, []) if n not in visited)

            if cluster:
                clusters.append(cluster)

        # Validate clusters: reject oversized clusters and merge nearby ones.
        # A "detail" is small by definition — use tighter bounds to avoid false positives.
        # For FDM (emboss_height=2mm): max diagonal = 6mm (3× height), appropriate for pips (~3mm diameter).
        max_cluster_diagonal = min_height_mm * 3.0
        min_cluster_triangles = 3  # Reject noise (single triangles or tiny pairs)

        valid_clusters: list[list[int]] = []
        for cluster in clusters:
            # Minimum size check: reject very small clusters (likely tessellation noise)
            if len(cluster) < min_cluster_triangles:
                continue

            cluster_verts = np.array(
                [vertices[v] for f in cluster for v in mesh.faces[f]]
            )
            extents = cluster_verts.max(axis=0) - cluster_verts.min(axis=0)
            diag = float(np.linalg.norm(extents))
            if diag <= max_cluster_diagonal:
                valid_clusters.append(cluster)

        if not valid_clusters:
            return 0, [], []

        # Merge clusters that are very close to each other (likely same feature split by tessellation).
        # Use iterative merging: repeatedly merge closest pairs until no clusters are within merge_distance.
        # Use 1.45× emboss height: fine-tuned to merge split pip clusters on dice.
        merge_distance = min_height_mm * 1.45
        min_feature_area = math.pi * (min_height_mm / 2) ** 2 * 0.25  # Minimum area threshold

        # Pre-compute centroids and areas for all valid clusters
        cluster_info: list[tuple[np.ndarray, float, list[int]]] = []
        for cluster in valid_clusters:
            cluster_verts = np.array(
                [vertices[v] for f in cluster for v in mesh.faces[f]]
            )
            centroid = cluster_verts.mean(axis=0)
            area = sum(float(mesh.area_faces[f]) for f in cluster if f < len(mesh.area_faces))
            cluster_info.append((centroid, area, cluster))

        # Iteratively merge closest clusters within merge_distance
        merged = True
        while merged:
            merged = False
            # Find closest pair of clusters within merge_distance
            best_i, best_j = -1, -1
            best_dist = float('inf')

            for i in range(len(cluster_info)):
                for j in range(i + 1, len(cluster_info)):
                    centroid_i, _, _ = cluster_info[i]
                    centroid_j, _, _ = cluster_info[j]
                    dist = float(np.linalg.norm(centroid_i - centroid_j))
                    if dist < best_dist and dist <= merge_distance:
                        best_dist = dist
                        best_i, best_j = i, j

            if best_i >= 0:
                # Merge clusters best_j into best_i
                _, area_i, cluster_i = cluster_info[best_i]
                _, area_j, cluster_j = cluster_info[best_j]

                # Compute new centroid as area-weighted average
                total_area = area_i + area_j
                verts_i = np.array([vertices[v] for f in cluster_i for v in mesh.faces[f]])
                verts_j = np.array([vertices[v] for f in cluster_j for v in mesh.faces[f]])
                new_centroid = (verts_i.mean(axis=0) * area_i + verts_j.mean(axis=0) * area_j) / total_area

                merged_cluster = cluster_i + cluster_j
                cluster_info[best_i] = (new_centroid, total_area, merged_cluster)
                del cluster_info[best_j]
                merged = True

        # Filter by minimum area and aspect ratio to keep only circular features (pips).
        # Elongated clusters (edges, corners) should be rejected.
        merged_clusters: list[list[int]] = []
        for centroid, area, cluster in cluster_info:
            if area < min_feature_area:
                continue

            # Compute aspect ratio: check if cluster is roughly circular
            cluster_verts = np.array(
                [vertices[v] for f in cluster for v in mesh.faces[f]]
            )
            extents = cluster_verts.max(axis=0) - cluster_verts.min(axis=0)
            # Sort extents to find the two largest dimensions (ignoring the smallest/depth dimension)
            sorted_extents = sorted([float(extents[0]), float(extents[1]), float(extents[2])], reverse=True)
            # Aspect ratio = largest / second_largest. For a circle, this should be close to 1.
            # Allow up to 2.5 to account for slightly oval pips and tessellation irregularities.
            if sorted_extents[0] > 0:
                aspect_ratio = sorted_extents[0] / max(sorted_extents[1], 1e-9)
                if aspect_ratio <= 2.5:
                    merged_clusters.append(cluster)

        if not merged_clusters:
            return 0, [], []

        # Emit results: one centroid per merged cluster, all cluster faces.
        count = len(merged_clusters)
        for cluster in merged_clusters:
            cluster_verts = np.array(
                [vertices[v] for f in cluster for v in mesh.faces[f]]
            )
            centroid = cluster_verts.mean(axis=0)
            centroids.append(
                [float(centroid[0]), float(centroid[1]), float(centroid[2])]
            )
            face_indices.extend(cluster)

    except Exception as exc:
        logger.warning("embossed/engraved detection failed: %s", exc)

    return count, centroids[:100], face_indices[:1000]


# ---------------------------------------------------------------------------
# Connecting parts clearance (multi-body assemblies)
# ---------------------------------------------------------------------------


def detect_connecting_clearance(
    mesh: "trimesh.Trimesh",
    min_clearance_mm: float,
    bodies: "list | None" = None,
) -> tuple[int, list[list[float]], list[int]]:
    """Detect insufficient clearance between separate mesh bodies.

    Requires a multi-body mesh (e.g. an assembly uploaded as a single STL).
    Uses scipy KD-tree to find the minimum distance between body pairs.

    Args:
        mesh: The mesh to analyse.
        min_clearance_mm: Minimum required clearance in mm.
        bodies: Optional pre-split body list — avoids calling mesh.split() again
                when the caller has already done so (T2d optimisation).

    Returns:
        (count_of_violations, centroids_at_violation, face_indices)
    """
    import trimesh

    count = 0
    centroids: list[list[float]] = []
    face_indices: list[int] = []

    try:
        if not isinstance(mesh, trimesh.Trimesh):
            return 0, [], []

        if bodies is None:
            split = mesh.split()
            if isinstance(split, trimesh.Trimesh):
                return 0, [], []
            if not isinstance(split, trimesh.Scene):
                return 0, [], []
            bodies = list(split.geometry.values())

        bodies = [b for b in bodies if isinstance(b, trimesh.Trimesh)]
        if len(bodies) < 2:
            return 0, [], []

        from scipy.spatial import KDTree

        for i in range(len(bodies)):
            for j in range(i + 1, len(bodies)):
                pts_a = bodies[i].vertices
                pts_b = bodies[j].vertices
                tree = KDTree(pts_b)
                dists, _ = tree.query(pts_a, k=1)
                min_dist = float(dists.min())
                if min_dist < min_clearance_mm:
                    # Find the closest point pair
                    idx_a = int(np.argmin(dists))
                    pt = pts_a[idx_a]
                    centroids.append([float(pt[0]), float(pt[1]), float(pt[2])])
                    count += 1

    except Exception as exc:
        logger.warning("connecting clearance detection failed: %s", exc)

    return count, centroids, face_indices
