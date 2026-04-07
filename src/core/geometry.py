import asyncio
import contextlib
import io
import logging
import math
import os
import shutil
import signal
import sys
import tempfile
import threading
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, TypedDict, cast

import gmsh
import numpy as np
import trimesh
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

THIN_WALL_THRESHOLD_MM = 0.8
OVERHANG_ANGLE_THRESHOLD_DEGREES = 45.0

# Timeout for gmsh mesh generation (seconds). If exceeded, the worker process
# is killed forcefully so the consumer can move on and retry or fail gracefully.
GMSH_MESH_TIMEOUT_SECONDS = 120


def to_camel(string: str) -> str:
    components = string.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


class BoundingBox(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    x: float
    y: float
    z: float


class GeometryMetrics(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    volume_cm3: float = Field(alias="volumeCm3")
    support_volume_cm3: float = Field(alias="supportVolumeCm3")
    surface_area_cm2: float = Field(alias="surfaceAreaCm2")
    bounding_box: BoundingBox = Field(alias="boundingBox")
    is_manifold: bool = Field(alias="isManifold")
    triangle_count: int = Field(alias="triangleCount")
    euler_number: int = Field(alias="eulerNumber")


class DfmIssueItem(BaseModel):
    """A single DFM issue with human-readable description and threshold context.

    Included in each DFM report payload so the frontend can display backend-generated
    descriptions without duplicating threshold values.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    category: str
    severity: str = "warning"
    title: str
    description: str
    value: float | None = None
    threshold: float | None = None


class FdmDfmReport(BaseModel):
    """DFM analysis results specific to FDM 3D printing."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    report_type: str = Field(default="FDM", alias="reportType")
    thin_wall_count: int = Field(alias="thinWallCount")
    thin_wall_regions: list[list[float]] = Field(alias="thinWallRegions")
    overhang_face_count: int = Field(alias="overhangFaceCount")
    overhang_area_cm2: float = Field(alias="overhangAreaCm2")
    overhang_regions: list[list[float]] = Field(alias="overhangRegions")
    support_required: bool = Field(alias="supportRequired")
    estimated_support_volume_cm3: float | None = Field(
        default=None, alias="estimatedSupportVolumeCm3"
    )
    small_detail_count: int = Field(default=0, alias="smallDetailCount")
    issues: list[DfmIssueItem] = Field(default_factory=list)


class SlaDfmReport(BaseModel):
    """DFM analysis results specific to SLA/DLP resin printing."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    report_type: str = Field(default="SLA", alias="reportType")
    thin_wall_count: int = Field(alias="thinWallCount")
    thin_wall_regions: list[list[float]] = Field(alias="thinWallRegions")
    overhang_face_count: int = Field(alias="overhangFaceCount")
    overhang_area_cm2: float = Field(alias="overhangAreaCm2")
    overhang_regions: list[list[float]] = Field(alias="overhangRegions")
    resin_trapping_risk: bool = Field(alias="resinTrappingRisk")
    resin_trapping_regions: list[list[float]] = Field(alias="resinTrappingRegions")
    suction_risk: bool = Field(alias="suctionRisk")
    suction_regions: list[list[float]] = Field(alias="suctionRegions")
    hollow_regions: list[list[float]] = Field(alias="hollowRegions")
    issues: list[DfmIssueItem] = Field(default_factory=list)


class CncDfmReport(BaseModel):
    """DFM analysis results specific to CNC machining."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    report_type: str = Field(default="CNC", alias="reportType")
    sharp_corner_count: int = Field(alias="sharpCornerCount")
    sharp_corner_regions: list[list[float]] = Field(alias="sharpCornerRegions")
    has_undercuts: bool = Field(alias="hasUndercuts")
    undercut_regions: list[list[float]] = Field(alias="undercutRegions")
    has_drill_holes: bool = Field(alias="hasDrillHoles")
    drill_hole_count: int = Field(default=0, alias="drillHoleCount")
    requires_edm: bool = Field(default=False, alias="requiresEdm")
    requires_grinding: bool = Field(default=False, alias="requiresGrinding")
    minimum_feature_size_mm: float = Field(default=1.0, alias="minimumFeatureSizeMm")
    issues: list[DfmIssueItem] = Field(default_factory=list)


def _compute_thin_wall_analysis(mesh: trimesh.Trimesh) -> tuple[int, list[list[float]]]:
    """
    Detect thin-wall regions where wall thickness is below threshold.
    Uses mesh unique edges to estimate local wall thickness.
    Only reports clusters of ≥3 connected faces to filter tessellation artifacts.
    Returns (thin_wall_count, thin_wall_centroids).
    """
    thin_wall_count = 0
    thin_wall_centroids: list[list[float]] = []

    try:
        if len(mesh.faces) < 3:
            return 0, []

        vertices = mesh.vertices
        unique_edges = mesh.edges_unique

        edge_lengths: list[tuple[float, tuple[int, int], np.ndarray]] = []
        for edge in unique_edges:
            v0, v1 = vertices[edge[0]], vertices[edge[1]]
            length = float(np.linalg.norm(v1 - v0))
            if length < THIN_WALL_THRESHOLD_MM:
                mid = (v0 + v1) / 2
                edge_lengths.append((length, (int(edge[0]), int(edge[1])), mid))

        if not edge_lengths:
            return 0, []

        edge_to_faces: dict[tuple[int, int], list[int]] = {}
        for idx, face in enumerate(mesh.faces):
            for k in range(3):
                key = (int(face[k]), int(face[(k + 1) % 3]))
                key = (min(key), max(key))
                edge_to_faces.setdefault(key, []).append(idx)

        thin_face_indices: set[int] = set()
        for _length, edge, _mid in edge_lengths:
            for fidx in edge_to_faces.get(edge, []):
                thin_face_indices.add(fidx)

        if not thin_face_indices:
            return 0, []

        face_adj: dict[int, list[int]] = {f: [] for f in thin_face_indices}
        for fidx in thin_face_indices:
            face = mesh.faces[fidx]
            for k in range(3):
                key = (
                    min(int(face[k]), int(face[(k + 1) % 3])),
                    max(int(face[k]), int(face[(k + 1) % 3])),
                )
                neighbors = edge_to_faces.get(key, [])
                for n in neighbors:
                    if n in thin_face_indices and n != fidx:
                        face_adj[fidx].append(n)

        visited: set[int] = set()
        valid_clusters: list[list[int]] = []
        for start in thin_face_indices:
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
            if len(cluster) >= 3:
                valid_clusters.append(cluster)

        for cluster in valid_clusters:
            cluster_verts = np.array(
                [vertices[v] for f in cluster for v in mesh.faces[f]]
            )
            centroid = cluster_verts.mean(axis=0)
            thin_wall_centroids.append(
                [float(centroid[0]), float(centroid[1]), float(centroid[2])]
            )
            thin_wall_count += len(cluster)

    except Exception as e:
        logger.warning(f"Thin wall analysis failed: {e}")

    return thin_wall_count, thin_wall_centroids[:100]


def _compute_overhang_analysis(
    mesh: trimesh.Trimesh,
) -> tuple[int, float, list[list[float]]]:
    """
    Detect overhang faces where the normal deviates more than threshold from vertical (Z-axis).
    Returns (overhang_face_count, overhang_area_cm2, overhang_centroids).
    """
    overhang_face_count = 0
    overhang_area_cm2 = 0.0
    overhang_centroids: list[list[float]] = []

    try:
        if len(mesh.faces) < 3:
            return 0, 0.0, []

        face_normals = mesh.face_normals
        z_axis = np.array([0.0, 0.0, 1.0])
        threshold_cos = math.cos(math.radians(OVERHANG_ANGLE_THRESHOLD_DEGREES))

        face_areas = mesh.area_faces
        face_centroids = mesh.triangles_center

        overhang_mask = []
        for i, normal in enumerate(face_normals):
            normal_unit = normal / (np.linalg.norm(normal) + 1e-10)
            dot_product = float(np.dot(normal_unit, z_axis))
            is_overhang = dot_product < threshold_cos
            overhang_mask.append(is_overhang)

            if is_overhang:
                overhang_face_count += 1
                area_cm2 = float(face_areas[i]) / 100.0
                overhang_area_cm2 += area_cm2
                centroid = face_centroids[i]
                overhang_centroids.append(
                    [float(centroid[0]), float(centroid[1]), float(centroid[2])]
                )

    except Exception as e:
        logger.warning(f"Overhang analysis failed: {e}")

    return overhang_face_count, overhang_area_cm2, overhang_centroids[:100]


def _compute_sharp_corner_analysis(
    mesh: trimesh.Trimesh,
) -> tuple[int, list[list[float]]]:
    """
    Detect sharp corners that could cause tool breakage in CNC machining.
    Uses edge angles at vertices to identify sharp corners.
    Returns (sharp_corner_count, sharp_corner_coordinates).
    """
    sharp_corner_count = 0
    sharp_corners: list[list[float]] = []

    try:
        if len(mesh.edges) < 3:
            return 0, []

        vertices = mesh.vertices
        SHARP_CORNER_THRESHOLD_DEGREES = 45.0
        threshold_cos = math.cos(math.radians(SHARP_CORNER_THRESHOLD_DEGREES))

        edge_directions: dict[int, list[np.ndarray]] = {}

        for edge in mesh.edges:
            v0_idx, v1_idx = int(edge[0]), int(edge[1])
            v0, v1 = vertices[v0_idx], vertices[v1_idx]
            direction = (v1 - v0) / (np.linalg.norm(v1 - v0) + 1e-10)

            if v0_idx not in edge_directions:
                edge_directions[v0_idx] = []
            edge_directions[v0_idx].append(direction)

            if v1_idx not in edge_directions:
                edge_directions[v1_idx] = []
            edge_directions[v1_idx].append(-direction)

        for vertex_idx, directions in edge_directions.items():
            if len(directions) < 2:
                continue

            for i in range(len(directions)):
                for j in range(i + 1, len(directions)):
                    d1, d2 = directions[i], directions[j]
                    dot = float(np.clip(np.dot(d1, d2), -1.0, 1.0))
                    angle_cos = dot

                    if angle_cos < threshold_cos:
                        vertex = vertices[vertex_idx]
                        sharp_corners.append(
                            [float(vertex[0]), float(vertex[1]), float(vertex[2])]
                        )
                        sharp_corner_count += 1
                        break

    except Exception as e:
        logger.warning(f"Sharp corner analysis failed: {e}")

    return sharp_corner_count, sharp_corners[:50]


def _compute_hollow_analysis(mesh: trimesh.Trimesh) -> list[list[float]]:
    """
    Detect hollow regions that may need drainage holes in SLA printing.
    Uses mesh splitting to identify enclosed cavities.
    Returns list of hollow region centroids.
    """
    hollow_regions: list[list[float]] = []

    try:
        if not mesh.is_watertight:
            return hollow_regions

        split = mesh.split()

        if isinstance(split, trimesh.Trimesh):
            return hollow_regions

        if isinstance(split, trimesh.Scene):
            if len(split.geometry) > 1:
                for body in split.geometry.values():
                    if isinstance(body, trimesh.Trimesh) and not body.is_watertight:
                        centroid = body.centroid
                        hollow_regions.append(
                            [float(centroid[0]), float(centroid[1]), float(centroid[2])]
                        )

    except Exception as e:
        logger.warning(f"Hollow analysis failed: {e}")

    return hollow_regions[:50]


class ViewConfig(TypedDict):
    """Configuration for a single camera view."""

    camera_dir: tuple[float, float, float]
    viewup: tuple[float, float, float]
    distance_padding: float


VIEW_CONFIGS: dict[str, ViewConfig] = {
    # camera_dir is the unit offset from center where the camera is placed.
    # Camera looks from (center + camera_dir * distance) toward center.
    # distance_padding scales the base distance — smaller = closer to the object.
    "front": {"camera_dir": (0, -1, 0), "viewup": (0, 0, 1), "distance_padding": 1.3},
    "back": {"camera_dir": (0, 1, 0), "viewup": (0, 0, 1), "distance_padding": 1.3},
    "left": {"camera_dir": (-1, 0, 0), "viewup": (0, 0, 1), "distance_padding": 1.3},
    "right": {"camera_dir": (1, 0, 0), "viewup": (0, 0, 1), "distance_padding": 1.3},
    "top": {"camera_dir": (0, 0, 1), "viewup": (0, 1, 0), "distance_padding": 1.3},
    "bottom": {"camera_dir": (0, 0, -1), "viewup": (0, -1, 0), "distance_padding": 1.3},
    "iso": {"camera_dir": (1, -1, 0.5), "viewup": (0, 0, 1), "distance_padding": 1.05},
}

ORTHO_VIEWS = ["front", "back", "left", "right", "top", "bottom"]
ISOMETRIC_VIEWS = ["iso"]
ALL_VIEWS = ORTHO_VIEWS + ISOMETRIC_VIEWS
DEFAULT_SIZE = 256


def _render_single_view(
    pv_mesh: Any,
    feature_edges: Any,
    center: np.ndarray[Any, np.dtype[Any]],
    max_dim: float,
    camera_dir: tuple[float, float, float],
    viewup: tuple[float, float, float],
    size: int,
    distance_padding: float = 1.3,
    fmt: str = "PNG",
    quality: int | None = None,
) -> bytes | None:
    import math

    import pyvista as pv
    from PIL import Image

    # Initialize virtual framebuffer for headless rendering in Docker
    pv.start_xvfb()

    # 85mm lens equivalent: vertical FOV = 2*atan(24/(2*85)) ≈ 16°
    view_angle = 16.0

    # Compute distance so the model's bounding sphere fills the view with padding.
    # half_fov = 8°, so distance = (max_dim/2) / tan(8°) * padding
    distance = (
        (max_dim / 2.0) / math.tan(math.radians(view_angle / 2.0)) * distance_padding
    )

    pl = pv.Plotter(off_screen=True, window_size=[size, size], lighting=None)
    pl.set_background("#FFFFFF")
    pl.enable_anti_aliasing("msaa")

    # Shaded body mesh
    pl.add_mesh(
        pv_mesh,
        color="#D4D4D4",
        smooth_shading=True,
        specular=0.02,
        specular_power=8,
        ambient=0.55,
        diffuse=0.45,
    )

    # CAD feature edges — sharp boundary lines only, no tessellation artifacts
    if feature_edges is not None and feature_edges.n_lines > 0:
        pl.add_mesh(
            feature_edges,
            color="#4A4A4A",
            line_width=1.5,
            lighting=False,
        )

    # Place camera at center + direction * distance, looking back at center
    cam_pos = center + np.array(camera_dir) * distance
    focal_point = center

    pl.camera_position = [
        tuple(cam_pos),
        tuple(focal_point),
        tuple(viewup),
    ]

    # Apply FOV — must be set after camera_position
    pl.camera.view_angle = view_angle

    d = max_dim * 4

    key_light = pv.Light(
        position=(center[0] + d, center[1] - d * 0.5, center[2] + d),
        focal_point=tuple(center),
        intensity=0.35,
    )
    pl.add_light(key_light)

    fill_light = pv.Light(
        position=(center[0] - d, center[1] - d * 0.3, center[2] + d * 0.8),
        focal_point=tuple(center),
        intensity=0.30,
    )
    pl.add_light(fill_light)

    front_fill = pv.Light(
        position=tuple(cam_pos),
        focal_point=tuple(center),
        intensity=0.20,
    )
    pl.add_light(front_fill)

    img = pl.screenshot(transparent_background=False, return_img=True)
    pl.close()

    pil_img = Image.fromarray(img)
    buf = io.BytesIO()
    save_kwargs: dict[str, Any] = {"format": fmt}
    if quality is not None:
        save_kwargs["quality"] = quality
    pil_img.save(buf, **save_kwargs)
    buf.seek(0)
    return buf.getvalue()


def _generate_preview_images_sync(mesh: trimesh.Trimesh) -> dict[str, bytes | None]:
    """
    Generate 7 preview images (6 orthographic + 1 isometric) as WebP.
    Uses VTK/OSMesa for headless CPU rendering (no GPU required).
    """
    results: dict[str, bytes | None] = {}
    empty: dict[str, bytes | None] = {f"{view}_small": None for view in ORTHO_VIEWS}
    empty["thumbnail_small"] = None
    empty["thumbnail_large"] = None

    try:
        import pyvista as pv

        pv.OFF_SCREEN = True
    except Exception as e:
        logger.error(f"Failed to import pyvista: {e}")
        return empty

    try:
        if len(mesh.faces) == 0:
            logger.warning("Mesh has no polygon faces — skipping preview generation")
            return empty

        mesh.process()
        # Center mesh at origin so camera directions are axis-aligned
        mesh.vertices -= mesh.centroid

        faces_pv = np.column_stack(
            [np.full(len(mesh.faces), 3, dtype=np.int32), mesh.faces]
        ).ravel()
        pv_mesh = pv.PolyData(mesh.vertices.copy(), faces_pv)

        # Extract feature edges BEFORE split_vertices destroys adjacency info.
        # split_vertices=False keeps shared vertices so adjacent face angles
        # can be compared for feature edge detection.
        pv_mesh.compute_normals(
            cell_normals=True, point_normals=True, split_vertices=False, inplace=True
        )
        feature_edges = pv_mesh.extract_feature_edges(
            boundary_edges=False,
            feature_edges=True,
            manifold_edges=False,
            non_manifold_edges=False,
            feature_angle=30,
        )

        # Recompute with split_vertices=True for smooth per-face shading
        pv_mesh.compute_normals(
            cell_normals=True, point_normals=True, split_vertices=True, inplace=True
        )

        center = np.zeros(3)  # mesh is now at origin
        bounds = np.array(pv_mesh.bounds)
        dims = np.array(
            [bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]]
        )
        max_dim = float(np.max(dims))

        for view_name in ALL_VIEWS:
            config = VIEW_CONFIGS[view_name]
            result_key = (
                "thumbnail_small" if view_name == "iso" else f"{view_name}_small"
            )
            try:
                image_bytes = _render_single_view(
                    pv_mesh=pv_mesh,
                    feature_edges=feature_edges,
                    center=center,
                    max_dim=max_dim,
                    camera_dir=config["camera_dir"],
                    viewup=config["viewup"],
                    size=DEFAULT_SIZE,
                    distance_padding=config["distance_padding"],
                    fmt="WEBP",
                    quality=80,
                )
                results[result_key] = image_bytes
            except Exception as e:
                logger.error(
                    f"Failed to generate preview image for view {view_name}: {e}"
                )
                results[result_key] = None

        # 1200px ISO WebP — hi-res fallback for the detail card viewer
        try:
            iso_config = VIEW_CONFIGS["iso"]
            results["thumbnail_large"] = _render_single_view(
                pv_mesh=pv_mesh,
                feature_edges=feature_edges,
                center=center,
                max_dim=max_dim,
                camera_dir=iso_config["camera_dir"],
                viewup=iso_config["viewup"],
                size=1200,
                distance_padding=iso_config["distance_padding"],
                fmt="WEBP",
                quality=80,
            )
        except Exception as e:
            logger.error(f"Failed to generate 1200px ISO WebP preview: {e}")
            results["thumbnail_large"] = None

    except Exception as e:
        logger.error(f"Failed to generate preview images: {e}")
        return empty

    for key in empty:
        if key not in results:
            results[key] = None

    return results


def _run_gmsh_with_timeout(file_path: str, timeout_seconds: int) -> trimesh.Trimesh:
    """
    Runs gmsh mesh generation with a timeout. If gmsh hangs or crashes,
    the worker process is killed forcefully so the consumer can retry.

    Uses signal.SIGALRM on Unix and a watchdog thread on Windows.
    """
    mesh = None
    timeout_fired = False

    def _sigalarm_handler(signum, frame):
        nonlocal timeout_fired
        timeout_fired = True
        # Attempt graceful gmsh finalize before os._exit
        try:
            gmsh.finalize()
        except Exception:
            pass
        # Log before dying so Aspire structured logs capture it
        logger.error(
            f"gmsh mesh generation timed out after {timeout_seconds}s — killing worker process",
            extra={
                "event": "gmsh_timeout",
                "timeout_seconds": timeout_seconds,
                "file": file_path,
            },
        )
        # os._exit bypasses Python cleanup but ensures the process dies immediately.
        # ProcessPoolExecutor will detect the abnormal exit and raise BrokenProcessPool.
        os._exit(1)

    if sys.platform != "win32":
        # Unix: use signal.SIGALRM for accurate timeout that can interrupt gmsh C code
        old_handler = signal.signal(signal.SIGALRM, _sigalarm_handler)
        signal.alarm(timeout_seconds)
        try:
            gmsh.initialize()
            gmsh.option.setNumber("General.Verbosity", 0)
            gmsh.open(file_path)
            # Use 0.1mm mesh to balance smooth curve tessellation with reasonable
            # performance for both DFM analysis and GLB rendering quality.
            gmsh.option.setNumber("Mesh.CharacteristicLengthMax", 0.1)
            gmsh.option.setNumber("Mesh.AngleSmoothNormals", 0.30)
            # Enable geometry healing for problematic STEP files
            gmsh.option.setNumber("Geometry.OCCFixDegenerated", 1)
            gmsh.option.setNumber("Geometry.OCCFixSmallEdges", 1)
            gmsh.option.setNumber("Geometry.OCCFixSmallFaces", 1)
            gmsh.option.setNumber("Geometry.OCCSewFaces", 1)
            gmsh.model.mesh.generate(2)

            _, coords, _ = gmsh.model.mesh.getNodes()
            v = np.array(coords).reshape((-1, 3))

            _, _, node_tags = gmsh.model.mesh.getElements(2)

            if len(node_tags) > 0:
                f = np.array(node_tags[0]) - 1
                mesh = trimesh.Trimesh(vertices=v, faces=f.reshape((-1, 3)))
            else:
                raise ValueError("GMSH_TESSELLATION_FAILED: No triangles")
        finally:
            signal.alarm(0)  # Cancel any pending alarm
            signal.signal(signal.SIGALRM, old_handler)
            gmsh.finalize()
    else:
        # Windows: signal.SIGALRM is not available; use a watchdog thread + gmsh in a
        # subprocess.  If the thread fires before gmsh returns, we terminate the process.
        result = {}

        def _gmsh_work():
            try:
                gmsh.initialize()
                gmsh.option.setNumber("General.Verbosity", 0)
                gmsh.open(file_path)
                # Use 0.1mm mesh to balance smooth curve tessellation with reasonable
                # performance for both DFM analysis and GLB rendering quality.
                gmsh.option.setNumber("Mesh.CharacteristicLengthMax", 0.1)
                gmsh.option.setNumber("Mesh.AngleSmoothNormals", 0.30)
                # Enable geometry healing for problematic STEP files
                gmsh.option.setNumber("Geometry.OCCFixDegenerated", 1)
                gmsh.option.setNumber("Geometry.OCCFixSmallEdges", 1)
                gmsh.option.setNumber("Geometry.OCCFixSmallFaces", 1)
                gmsh.option.setNumber("Geometry.OCCSewFaces", 1)
                gmsh.model.mesh.generate(2)

                _, coords, _ = gmsh.model.mesh.getNodes()
                v = np.array(coords).reshape((-1, 3))

                _, _, node_tags = gmsh.model.mesh.getElements(2)

                if len(node_tags) > 0:
                    f = np.array(node_tags[0]) - 1
                    mesh = trimesh.Trimesh(vertices=v, faces=f.reshape((-1, 3)))
                else:
                    mesh = None
                result["mesh"] = mesh
            except Exception as e:
                result["error"] = e
            finally:
                try:
                    gmsh.finalize()
                except Exception:
                    pass

        worker_thread = threading.Thread(target=_gmsh_work, daemon=True)
        worker_thread.start()
        worker_thread.join(timeout=timeout_seconds)

        if worker_thread.is_alive() or "error" in result:
            # gmsh is still running or raised an error — kill this process
            logger.error(
                f"gmsh mesh generation timed out after {timeout_seconds}s (Windows) — killing worker process",
                extra={
                    "event": "gmsh_timeout",
                    "timeout_seconds": timeout_seconds,
                    "file": file_path,
                },
            )
            os._exit(1)

        mesh = result.get("mesh")
        if mesh is None:
            raise ValueError("GMSH_TESSELLATION_FAILED: No triangles")

    return mesh


def _load_cad_with_cascadio(
    file_path: str, timeout_seconds: int = 60
) -> tuple[trimesh.Trimesh, bytes, int]:
    """
    Load STEP/IGES file via cascadio (OpenCascade) with 0.1mm linear deviation
    and 0.5 rad angular deviation for smooth circles and fillets.
    Returns (mesh, glb_bytes) — the trimesh for metrics and the cascadio-produced
    GLB bytes for direct upload (avoids re-tessellating in Phase 2).
    Uses a thread-based timeout; the executor is shut down without waiting
    if cascadio hangs in C-extension code.
    """
    import concurrent.futures

    try:
        import cascadio
    except ImportError as exc:
        raise ImportError(
            "cascadio is required for CAD file loading. "
            "Install with: pip install cascadio"
        ) from exc

    def _do_load() -> tuple[trimesh.Trimesh, bytes]:
        import os
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as tmpdir:
            glb_path = os.path.join(tmpdir, "output.glb")
            ret = cascadio.step_to_glb(
                file_path,
                glb_path,
                tol_linear=0.02,
                tol_angular=0.05,
            )
            if ret != 0:
                raise ValueError(
                    f"cascadio.step_to_glb returned error code {ret} for {file_path}"
                )
            if not os.path.exists(glb_path):
                raise ValueError(
                    f"cascadio.step_to_glb produced no output file for {file_path}"
                )
            with open(glb_path, "rb") as f:
                glb_bytes = f.read()
            loaded = trimesh.load(glb_path, force="mesh")
            if isinstance(loaded, trimesh.Scene):
                meshes = list(loaded.geometry.values())
                if not meshes:
                    raise ValueError(f"cascadio produced empty scene for {file_path}")
                body_count = len(meshes)
                mesh = trimesh.util.concatenate(meshes)
            elif isinstance(loaded, trimesh.Trimesh):
                body_count = 1
                mesh = loaded
            else:
                raise ValueError(f"cascadio produced unexpected type: {type(loaded)}")
            if len(mesh.vertices) == 0:
                raise ValueError(f"cascadio produced empty mesh for {file_path}")
            # glTF/GLB stores coordinates in meters (spec). Convert to mm so all
            # metric computation (extents, volume, area) is consistent with STL/OBJ input.
            # cad_glb_bytes stays in meters — correct for BabylonJS rendering.
            mesh.apply_scale(1000.0)
            return mesh, glb_bytes, body_count

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_do_load)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError(
            f"cascadio timed out after {timeout_seconds}s loading {file_path}"
        )
    finally:
        pool.shutdown(wait=False)  # do not block on hung C-extension thread


def _compute_metrics_worker(data: bytes, file_extension: str) -> dict[str, Any]:
    """
    Phase 1 worker: loads mesh, computes metrics, exports mesh to STL bytes.
    Returns metrics dict + 'mesh_stl_bytes' for Phase 2.
    Runs in a separate process.
    """
    file_stream = io.BytesIO(data)
    tmp_path: str | None = None
    try:
        file_stream.seek(0)
        ext = file_extension.strip(".").lower()

        mesh: trimesh.Trimesh | None = None
        cad_glb_bytes: bytes | None = None
        cad_body_count: int = 1
        if ext in ["igs", "iges", "step", "stp"]:
            try:
                # Write to a temp file that cascadio will open.
                # delete=False because we manage cleanup manually in the finally block.
                with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                    shutil.copyfileobj(file_stream, tmp)
                    tmp_path = tmp.name

                logger.info(
                    "Loading CAD file with cascadio (timeout=60s)",
                    extra={
                        "event": "cascadio_start",
                        "extension": ext,
                        "tmp_path": tmp_path,
                    },
                )
                mesh, cad_glb_bytes, cad_body_count = _load_cad_with_cascadio(
                    tmp_path, timeout_seconds=60
                )
                logger.info(
                    "cascadio tessellation complete",
                    extra={
                        "event": "cascadio_complete",
                        "extension": ext,
                        "triangle_count": len(mesh.faces) if mesh else 0,
                    },
                )
            except ValueError:
                raise
            except Exception as e:
                logger.error(
                    f"CAD_LOAD_ERROR during cascadio processing: {e}",
                    extra={
                        "event": "cascadio_error",
                        "extension": ext,
                        "error": str(e),
                    },
                )
                raise ValueError(f"CAD_LOAD_ERROR: {ext} ({str(e)})") from e
        else:
            mesh_data = trimesh.load(file_stream, file_type=ext, force="mesh")
            if isinstance(mesh_data, trimesh.Scene):
                if not mesh_data.geometry:
                    raise ValueError("EMPTY_FILE_ERROR")
                cad_body_count = len(mesh_data.geometry)
                # dump(concatenate=True) applies each body's scene-graph transform before
                # merging, so relative positions between bodies are preserved.
                dumped = mesh_data.dump(concatenate=True)
                if not isinstance(dumped, trimesh.Trimesh) or len(dumped.vertices) == 0:
                    raise ValueError("EMPTY_FILE_ERROR")
                mesh = dumped
            else:
                mesh = cast(trimesh.Trimesh, mesh_data)
            # glTF/GLB spec mandates meters — convert to mm for metric computation
            if ext in ("glb", "gltf"):
                mesh.apply_scale(1000.0)
                cad_glb_bytes = (
                    data  # uploaded GLB bytes stay in meters (correct for BabylonJS)
                )

        if mesh is None or not isinstance(mesh, trimesh.Trimesh):
            raise ValueError("FILE_CORRUPT")

        is_manifold = bool(mesh.is_watertight)

        if is_manifold:
            volume_mm3 = float(mesh.volume)
            area_mm2 = float(mesh.area)
        else:
            try:
                hull = mesh.convex_hull
                volume_mm3 = float(hull.volume)
                area_mm2 = float(hull.area)
            except Exception:
                volume_mm3 = 0.0
                area_mm2 = float(mesh.area)

        euler_number = int(mesh.euler_number)
        extents = mesh.extents
        if extents is None:
            bbox = {"x": 0.0, "y": 0.0, "z": 0.0}
            vol_bbox = 0.0
        else:
            bbox = {
                "x": float(extents[0]),
                "y": float(extents[1]),
                "z": float(extents[2]),
            }
            vol_bbox = float(extents[0]) * float(extents[1]) * float(extents[2])

        support_mm3 = max(0.0, vol_bbox - volume_mm3)

        mesh_stl_bytes: bytes | None = None
        try:
            mesh_stl_bytes = cast(bytes, mesh.export(file_type="stl"))
        except Exception as ex:
            logger.warning(
                "STL export failed for geometry, Phase 2 may produce degraded results: %s",
                ex,
            )
            if ext == "stl":
                mesh_stl_bytes = data  # fall back to original STL bytes

        logger.info(
            "Metrics computed successfully",
            extra={
                "event": "metrics_computed",
                "volume_cm3": volume_mm3 / 1000.0,
                "triangle_count": len(mesh.faces),
                "is_manifold": is_manifold,
            },
        )

        return {
            "volume_cm3": volume_mm3 / 1000.0,
            "support_volume_cm3": support_mm3 / 1000.0,
            "surface_area_cm2": area_mm2 / 100.0,
            "bounding_box": bbox,
            "is_manifold": is_manifold,
            "triangle_count": len(mesh.faces),
            "euler_number": euler_number,
            "body_count": cad_body_count,
            "mesh_stl_bytes": mesh_stl_bytes,
            "cad_glb_bytes": cad_glb_bytes,
        }

    except ValueError:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in _compute_metrics_worker: {e}",
            extra={
                "event": "worker_unexpected_error",
                "error": str(e),
                "traceback": traceback.format_exc(),
            },
        )
        raise ValueError(f"FILE_CORRUPT: {str(e)}") from e
    finally:
        # Clean up temp file even on crash. Uses os._exit path for timeout crashes
        # (which already called os._exit above), so this is safe.
        if tmp_path:
            with contextlib.suppress(OSError):
                if Path(tmp_path).exists():
                    Path(tmp_path).unlink()


def compute_metrics_trimesh_only(
    data_stream: io.BytesIO, file_extension: str
) -> dict[str, Any]:
    """
    Fallback metrics computation using ONLY trimesh (no gmsh).
    Used when gmsh crashes or times out on problematic STEP files.
    This function runs in the consumer's main process (not in an executor).
    """
    file_stream = (
        io.BytesIO(data_stream.read()) if hasattr(data_stream, "read") else data_stream
    )
    file_stream.seek(0)

    ext = file_extension.strip(".").lower()
    logger.info(
        f"FALLBACK: Loading {ext} with trimesh only (no gmsh)",
        extra={"event": "trimesh_fallback_start", "extension": ext},
    )

    mesh_data = trimesh.load(file_stream, file_type=ext, force="mesh")
    fallback_body_count: int = 1
    if isinstance(mesh_data, trimesh.Scene):
        if not mesh_data.geometry:
            raise ValueError("EMPTY_FILE_ERROR")
        fallback_body_count = len(mesh_data.geometry)
        dumped = mesh_data.dump(concatenate=True)
        if not isinstance(dumped, trimesh.Trimesh) or len(dumped.vertices) == 0:
            raise ValueError("EMPTY_FILE_ERROR")
        mesh = dumped
    else:
        mesh = cast(trimesh.Trimesh, mesh_data)

    if mesh is None or not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("FILE_CORRUPT")

    is_manifold = bool(mesh.is_watertight)

    if is_manifold:
        volume_mm3 = float(mesh.volume)
        area_mm2 = float(mesh.area)
    else:
        try:
            hull = mesh.convex_hull
            volume_mm3 = float(hull.volume)
            area_mm2 = float(hull.area)
        except Exception:
            volume_mm3 = 0.0
            area_mm2 = float(mesh.area)

    extents = mesh.extents
    if extents is None:
        bbox = {"x": 0.0, "y": 0.0, "z": 0.0}
        vol_bbox = 0.0
    else:
        bbox = {
            "x": float(extents[0]),
            "y": float(extents[1]),
            "z": float(extents[2]),
        }
        vol_bbox = float(extents[0]) * float(extents[1]) * float(extents[2])

    support_mm3 = max(0.0, vol_bbox - volume_mm3)
    euler_number = int(mesh.euler_number)

    # Simplified DFM reports (skip expensive centroid computations in fallback)
    thin_wall_count = 0
    overhang_face_count = 0
    sharp_corner_count = 0
    hollow_centroids: list[dict] = []

    fdm_dfm_report = {
        "reportType": "FDM",
        "thinWallCount": thin_wall_count,
        "thinWallRegions": [],
        "overhangFaceCount": overhang_face_count,
        "overhangAreaCm2": 0.0,
        "overhangRegions": [],
        "supportRequired": overhang_face_count > 0 or thin_wall_count > 0,
        "estimatedSupportVolumeCm3": support_mm3 / 1000.0
        if overhang_face_count > 0
        else None,
        "smallDetailCount": 0,
    }

    sla_dfm_report = {
        "reportType": "SLA",
        "thinWallCount": thin_wall_count,
        "thinWallRegions": [],
        "overhangFaceCount": overhang_face_count,
        "overhangAreaCm2": 0.0,
        "overhangRegions": [],
        "resinTrappingRisk": len(hollow_centroids) > 0,
        "resinTrappingRegions": hollow_centroids,
        "suctionRisk": False,
        "suctionRegions": [],
        "hollowRegions": hollow_centroids,
    }

    cnc_dfm_report = {
        "reportType": "CNC",
        "sharpCornerCount": sharp_corner_count,
        "sharpCornerRegions": [],
        "hasUndercuts": False,
        "undercutRegions": [],
        "hasDrillHoles": False,
        "drillHoleCount": 0,
        "requiresEdm": sharp_corner_count > 20,
        "requiresGrinding": False,
        "minimumFeatureSizeMm": 1.0,
    }

    # Export STL for Phase 2 (needed for GLB generation)
    mesh_stl_bytes: bytes | None = None
    try:
        mesh_stl_bytes = cast(bytes, mesh.export(file_type="stl"))
    except Exception as ex:
        logger.warning(
            "STL export failed for geometry, Phase 2 may produce degraded results: %s",
            ex,
        )

    logger.info(
        "FALLBACK: Metrics computed with trimesh only",
        extra={
            "event": "trimesh_fallback_complete",
            "extension": ext,
            "volume_cm3": volume_mm3 / 1000.0,
            "triangle_count": len(mesh.faces),
        },
    )

    return {
        "volume_cm3": volume_mm3 / 1000.0,
        "support_volume_cm3": support_mm3 / 1000.0,
        "surface_area_cm2": area_mm2 / 100.0,
        "bounding_box": bbox,
        "is_manifold": is_manifold,
        "triangle_count": len(mesh.faces),
        "euler_number": euler_number,
        "body_count": fallback_body_count,
        "mesh_stl_bytes": mesh_stl_bytes,
        "dfmReports": {
            "FDM": fdm_dfm_report,
            "SLA": sla_dfm_report,
            "CNC": cnc_dfm_report,
        },
    }


def _analyze_bytes_worker(data: bytes, file_extension: str) -> dict[str, Any]:
    """
    Worker function that runs in a separate process.
    Returns a dict to avoid pickle issues with custom classes.
    """
    file_stream = io.BytesIO(data)
    tmp_path = None
    try:
        file_stream.seek(0)
        ext = file_extension.strip(".").lower()

        mesh = None
        if ext in ["igs", "iges", "step", "stp"]:
            try:
                with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                    shutil.copyfileobj(file_stream, tmp)
                    tmp_path = tmp.name

                logger.info(
                    f"Loading CAD file with gmsh (timeout={GMSH_MESH_TIMEOUT_SECONDS}s)",
                    extra={
                        "event": "gmsh_start",
                        "extension": ext,
                        "tmp_path": tmp_path,
                    },
                )
                mesh = _run_gmsh_with_timeout(tmp_path, GMSH_MESH_TIMEOUT_SECONDS)
                logger.info(
                    "gmsh tessellation complete",
                    extra={
                        "event": "gmsh_complete",
                        "extension": ext,
                        "triangle_count": len(mesh.faces) if mesh else 0,
                    },
                )
            except ValueError:
                raise
            except Exception as e:
                logger.error(
                    f"CAD_LOAD_ERROR during gmsh processing: {e}",
                    extra={"event": "gmsh_error", "extension": ext, "error": str(e)},
                )
                raise ValueError(f"CAD_LOAD_ERROR: {ext} ({str(e)})") from e
            else:
                mesh_data = trimesh.load(file_stream, file_type=ext, force="mesh")
                if isinstance(mesh_data, trimesh.Scene):
                    if not mesh_data.geometry:
                        raise ValueError("EMPTY_FILE_ERROR")
                    dumped_mesh = mesh_data.dump(concatenate=True)
                    if not isinstance(dumped_mesh, trimesh.Trimesh) or len(dumped_mesh.vertices) == 0:
                        raise ValueError("EMPTY_FILE_ERROR")
                    mesh = dumped_mesh
                else:
                    mesh = cast(trimesh.Trimesh, mesh_data)
            # glTF/GLB spec mandates meters — convert to mm so all downstream
            # metric computation (volume, surface area, bounding box) is in mm.
            # The original cad_glb_bytes stays in meters for BabylonJS rendering.
            if ext in ("glb", "gltf"):
                mesh.apply_scale(1000.0)

        if mesh is None or not isinstance(mesh, trimesh.Trimesh):
            raise ValueError("FILE_CORRUPT")

        is_manifold = bool(mesh.is_watertight)

        if is_manifold:
            volume_mm3 = float(mesh.volume)
            area_mm2 = float(mesh.area)
        else:
            try:
                hull = mesh.convex_hull
                volume_mm3 = float(hull.volume)
                area_mm2 = float(hull.area)
            except Exception:
                volume_mm3 = 0.0
                area_mm2 = float(mesh.area)

        euler_number = int(mesh.euler_number)
        extents = mesh.extents
        if extents is None:
            bbox = {"x": 0.0, "y": 0.0, "z": 0.0}
            vol_bbox = 0.0
        else:
            bbox = {
                "x": float(extents[0]),
                "y": float(extents[1]),
                "z": float(extents[2]),
            }
            vol_bbox = float(extents[0]) * float(extents[1]) * float(extents[2])

        support_mm3 = max(0.0, vol_bbox - volume_mm3)

        glb_bytes: bytes | None = None

        with contextlib.suppress(Exception):
            glb_bytes = cast(bytes, mesh.export(file_type="glb"))

        preview_images: dict[str, bytes | None] = {}
        if len(mesh.faces) > 0:
            with contextlib.suppress(Exception):
                preview_images = _generate_preview_images_sync(mesh)
            if not is_manifold:
                logger.warning(
                    "Mesh is not manifold (not watertight) — previews generated with best-effort geometry"
                )
        else:
            logger.warning(
                "Mesh has no polygon faces — skipping preview generation (point cloud or line mesh)"
            )

        thumbnail_bytes = preview_images.get("thumbnail_small")

        return {
            "volume_cm3": volume_mm3 / 1000.0,
            "support_volume_cm3": support_mm3 / 1000.0,
            "surface_area_cm2": area_mm2 / 100.0,
            "bounding_box": bbox,
            "is_manifold": is_manifold,
            "triangle_count": len(mesh.faces),
            "euler_number": euler_number,
            "glb_bytes": glb_bytes,
            "thumbnail_bytes": thumbnail_bytes,
            "preview_images": preview_images,
        }

    except ValueError:
        raise
    except Exception as e:
        if "MULTI_BODY_ERROR" in str(e):
            raise
        raise ValueError(f"FILE_CORRUPT: {str(e)}") from e
    finally:
        if tmp_path and Path(tmp_path).exists():
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()


def _render_small_thumbnail_worker(stl_bytes: bytes) -> bytes | None:
    """Phase 2 worker: renders a 256px isometric thumbnail from STL bytes. Runs in a separate process."""
    try:
        import pyvista as pv

        pv.OFF_SCREEN = True

        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
            return None

        thumb_mesh = mesh.copy()
        thumb_mesh.vertices -= thumb_mesh.centroid
        faces_pv = np.column_stack(
            [np.full(len(thumb_mesh.faces), 3, dtype=np.int32), thumb_mesh.faces]
        ).ravel()
        pv_mesh_thumb = pv.PolyData(thumb_mesh.vertices.copy(), faces_pv)
        pv_mesh_thumb.compute_normals(
            cell_normals=True, point_normals=True, split_vertices=False, inplace=True
        )
        feat_edges = pv_mesh_thumb.extract_feature_edges(
            boundary_edges=False,
            feature_edges=True,
            manifold_edges=False,
            non_manifold_edges=False,
            feature_angle=30,
        )
        pv_mesh_thumb.compute_normals(
            cell_normals=True, point_normals=True, split_vertices=True, inplace=True
        )
        tb = np.array(pv_mesh_thumb.bounds)
        max_dim = float(np.max([tb[1] - tb[0], tb[3] - tb[2], tb[5] - tb[4]]))
        iso_cfg = VIEW_CONFIGS["iso"]
        return _render_single_view(
            pv_mesh=pv_mesh_thumb,
            feature_edges=feat_edges,
            center=np.zeros(3),
            max_dim=max_dim,
            camera_dir=iso_cfg["camera_dir"],
            viewup=iso_cfg["viewup"],
            size=DEFAULT_SIZE,
            distance_padding=iso_cfg["distance_padding"],
            fmt="WEBP",
            quality=80,
        )
    except Exception as e:
        logger.warning(f"_render_small_thumbnail_worker failed: {e}")
        return None


def _export_glb_worker(
    stl_bytes: bytes, cad_glb_bytes: bytes | None = None
) -> bytes | None:
    """Phase 2 worker: returns GLB bytes for the 3D viewer in Y-up, millimeters.

    Coordinate convention: all GLBs served to the viewer are **Y-up, mm**.
    The viewer (babylon-viewer.js) applies a +90° X rotation at load time to
    display the model in Z-up (CAD standard: X=right, Y=depth, Z=up).

    - STL/OBJ/3MF input: Z-up, mm. Pre-rotate −90°X to Y-up before export
      so the viewer's +90°X restores Z-up at display time.
    - STEP/IGES via cascadio: already Y-up, meters (glTF spec). Scale ×1000,
      center, export — no axis rotation needed.
    - Uploaded GLB/glTF: already Y-up, meters (glTF spec). Same as cascadio.
    """
    # Z-up → Y-up pre-rotation (applied to STL/native-Z-up meshes only).
    # −90° around X: (x, y, z) → (x, z, −y) — original Z (up) becomes glTF Y (up).
    z_to_yup = np.array(
        [[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=float
    )

    try:
        if cad_glb_bytes:
            # CAD GLB from cascadio (STEP/IGES) preserves OCCT-native Z-up orientation
            # rather than converting to glTF Y-up. Apply the same Z-up → Y-up pre-rotation
            # used by the STL path so both paths produce Y-up, mm GLBs for the viewer.
            # The viewer's +90°X load-time rotation will then restore Z-up for display.
            scene_data = trimesh.load(io.BytesIO(cad_glb_bytes), file_type="glb")
            if isinstance(scene_data, trimesh.Scene):
                mesh = scene_data.dump(concatenate=True)
            else:
                mesh = scene_data
            if not isinstance(mesh, trimesh.Trimesh) or len(mesh.vertices) == 0:
                return None
            mesh.apply_scale(1000)  # meters → mm
            mesh.apply_translation(-mesh.center_mass)  # center at origin
            mesh.apply_transform(z_to_yup)  # Z-up (OCCT) → Y-up (glTF)
            return cast(bytes, mesh.export(file_type="glb"))

        # STL path (from STL/OBJ/3MF uploads). Native Z-up, mm.
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            return None
        # Center to remove slicer build-plate offsets.
        mesh.apply_translation(-mesh.center_mass)
        # Pre-rotate Z-up → Y-up so the viewer's +90°X restores Z-up display.
        mesh.apply_transform(z_to_yup)
        # GLB stays in mm — no unit conversion.
        return cast(bytes, mesh.export(file_type="glb"))
    except Exception as e:
        logger.warning(f"_export_glb_worker failed: {e}")
        return None


def _compute_dfm_worker(
    stl_bytes: bytes,
    cad_bytes: bytes | None = None,
    cad_extension: str | None = None,
) -> dict[str, Any]:
    """Phase 3 worker: runs DFM analysis for all manufacturing processes.

    Returns a dict keyed by process code ("FDM", "SLA", "SLS", "MJF", "MJ",
    "BJ", "DMLS", "CNC_MILL", "CNC_TURN", plus legacy "CNC").
    Each value contains:
      - ``issues``: list of DfmIssue-style dicts with ``faceIndices`` for overlay
        GLB generation.
      - Legacy summary fields for backward-compat with existing C# consumers.

    Runs in a separate process via ProcessPoolExecutor.
    """
    try:
        from src.core.dfm_thresholds import (
            MILLING_RULES,
            PRINTING_RULES,
            TURNING_RULES,
            get_tool_for_radius,
        )
        from src.core.mesh_analyzers import (
            compute_overhang_analysis,
            compute_thin_wall_analysis,
            compute_unsupported_wall_analysis,
            detect_bridges,
            detect_connecting_clearance,
            detect_embossed_engraved,
            detect_escape_hole_risk,
            detect_holes_mesh,
            detect_hollow_regions,
            detect_small_features,
            detect_thin_pins,
        )
        from src.core.cnc_analyzers import (
            compute_sharp_corner_analysis,
            detect_axial_symmetry,
            detect_cavities,
            detect_chatter_risk,
            detect_deep_narrow_cavities,
            detect_grooves,
            detect_internal_radii,
            detect_tool_access,
        )
        from src.core.occ_analyzer import analyze_step_brep
    except ImportError as _imp_err:
        logger.warning("DFM analysis modules unavailable: %s", _imp_err)
        return {}

    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            return {}

        extents = (
            mesh.extents if mesh.extents is not None else np.array([0.0, 0.0, 0.0])
        )
        vol_bbox = float(extents[0]) * float(extents[1]) * float(extents[2])
        volume_mm3 = float(mesh.volume) if mesh.is_watertight else 0.0
        support_mm3 = max(0.0, vol_bbox - volume_mm3)

        # ── OCC B-Rep analysis (STEP/IGES only) ─────────────────────────────
        occ_features: list[Any] = []
        occ_face_tag_to_tri: dict[int, list[int]] = {}
        if cad_bytes and cad_extension in ("step", "stp", "igs", "iges"):
            try:
                occ_features, occ_face_tag_to_tri = analyze_step_brep(
                    cad_bytes, cad_extension
                )
            except Exception as _occ_err:
                logger.warning(
                    "OCC analysis failed — using mesh-only path: %s", _occ_err
                )

        # ── Pre-compute shared data ─────────────────────────────────────────
        hollow_centroids, _hollow_face_idx = detect_hollow_regions(mesh)
        all_holes = detect_holes_mesh(mesh, min_diameter_mm=1.0)

        reports: dict[str, Any] = {}

        # ── 3D Printing processes ─────────────────────────────────────────────
        for process_code, rules in PRINTING_RULES.items():
            issues: list[dict[str, Any]] = []

            # Thin wall (supported)
            tw_count, tw_centroids, tw_face_idx = compute_thin_wall_analysis(
                mesh, rules.supported_wall_mm
            )
            if tw_count > 0:
                issues.append(
                    {
                        "category": "thin_wall",
                        "severity": "warning",
                        "title": f"Thin Walls ({tw_count} regions)",
                        "description": (
                            f"{tw_count} wall region(s) below the {process_code} minimum"
                            f" of {rules.supported_wall_mm}mm."
                        ),
                        "value": float(tw_count),
                        "threshold": float(rules.supported_wall_mm),
                        "faceIndices": tw_face_idx,
                        "centroid": tw_centroids[0]
                        if tw_centroids
                        else [0.0, 0.0, 0.0],
                        "metadata": {},
                    }
                )

            # Unsupported wall (not applicable for powder-bed processes)
            uw_count, uw_centroids, uw_face_idx = 0, [], []
            if rules.unsupported_wall_mm is not None:
                uw_count, uw_centroids, uw_face_idx = compute_unsupported_wall_analysis(
                    mesh, rules.unsupported_wall_mm
                )
                if uw_count > 0:
                    issues.append(
                        {
                            "category": "unsupported_wall",
                            "severity": "warning",
                            "title": f"Unsupported Walls ({uw_count} regions)",
                            "description": (
                                f"{uw_count} unsupported wall(s) below"
                                f" {rules.unsupported_wall_mm}mm."
                            ),
                            "value": float(uw_count),
                            "threshold": float(rules.unsupported_wall_mm),
                            "faceIndices": uw_face_idx,
                            "centroid": uw_centroids[0]
                            if uw_centroids
                            else [0.0, 0.0, 0.0],
                            "metadata": {},
                        }
                    )

            # Overhang (not applicable for powder-bed processes)
            oh_count, oh_area_cm2, oh_centroids, oh_face_idx = 0, 0.0, [], []
            if rules.max_overhang_deg is not None:
                oh_count, oh_area_cm2, oh_centroids, oh_face_idx = (
                    compute_overhang_analysis(mesh, rules.max_overhang_deg)
                )
                if oh_count > 0:
                    issues.append(
                        {
                            "category": "overhang",
                            "severity": "warning",
                            "title": (
                                f"Overhangs ({oh_count} region(s),"
                                f" {oh_area_cm2:.1f}\u00a0cm\u00b2)"
                            ),
                            "description": (
                                f"{oh_count} overhang region(s) exceed the"
                                f" {rules.max_overhang_deg}° limit"
                                f" ({oh_area_cm2:.1f} cm² total area)."
                            ),
                            "value": float(oh_area_cm2),
                            "threshold": float(rules.max_overhang_deg),
                            "faceIndices": oh_face_idx,
                            "centroid": oh_centroids[0]
                            if oh_centroids
                            else [0.0, 0.0, 0.0],
                            "metadata": {
                                "areaCm2": float(oh_area_cm2),
                                "regionCount": oh_count,
                            },
                        }
                    )

            # Holes below process minimum diameter
            small_holes = [
                h for h in all_holes if h.diameter_mm < rules.min_hole_diameter_mm
            ]
            if small_holes:
                hole_face_idx: list[int] = []
                for h in small_holes:
                    hole_face_idx.extend(h.face_indices)
                min_diam = min(h.diameter_mm for h in small_holes)
                issues.append(
                    {
                        "category": "hole",
                        "severity": "warning",
                        "title": f"Small Holes ({len(small_holes)} features)",
                        "description": (
                            f"{len(small_holes)} hole(s) below the {process_code} minimum"
                            f" diameter of {rules.min_hole_diameter_mm}mm."
                            f" Smallest: Ø{min_diam:.2f}mm."
                        ),
                        "value": float(min_diam),
                        "threshold": float(rules.min_hole_diameter_mm),
                        "faceIndices": hole_face_idx[:2000],
                        "centroid": small_holes[0].center,
                        "metadata": {"holeCount": len(small_holes)},
                    }
                )

            # Bridges (only for processes where a span limit is defined)
            if rules.bridge_span_mm is not None:
                br_count, br_centroids, br_face_idx = detect_bridges(
                    mesh, rules.bridge_span_mm
                )
                if br_count > 0:
                    issues.append(
                        {
                            "category": "bridge",
                            "severity": "warning",
                            "title": f"Long Bridges ({br_count} spans)",
                            "description": (
                                f"{br_count} bridge span(s) exceed the {rules.bridge_span_mm}mm"
                                f" limit for {process_code}."
                            ),
                            "value": float(br_count),
                            "threshold": float(rules.bridge_span_mm),
                            "faceIndices": br_face_idx,
                            "centroid": br_centroids[0]
                            if br_centroids
                            else [0.0, 0.0, 0.0],
                            "metadata": {},
                        }
                    )

            # Small features
            sf_count, sf_centroids, sf_face_idx = detect_small_features(
                mesh, rules.min_feature_mm
            )
            if sf_count > 0:
                issues.append(
                    {
                        "category": "small_feature",
                        "severity": "warning",
                        "title": f"Small Features ({sf_count})",
                        "description": (
                            f"{sf_count} feature(s) smaller than the {process_code} minimum"
                            f" of {rules.min_feature_mm}mm."
                        ),
                        "value": float(sf_count),
                        "threshold": float(rules.min_feature_mm),
                        "faceIndices": sf_face_idx,
                        "centroid": sf_centroids[0]
                        if sf_centroids
                        else [0.0, 0.0, 0.0],
                        "metadata": {},
                    }
                )

            # Thin pins / columns
            pin_count, pin_centroids, pin_face_idx = detect_thin_pins(
                mesh, rules.pin_diameter_mm
            )
            if pin_count > 0:
                issues.append(
                    {
                        "category": "pin",
                        "severity": "warning",
                        "title": f"Thin Pins ({pin_count})",
                        "description": (
                            f"{pin_count} pin/column feature(s) below minimum diameter"
                            f" {rules.pin_diameter_mm}mm for {process_code}."
                        ),
                        "value": float(pin_count),
                        "threshold": float(rules.pin_diameter_mm),
                        "faceIndices": pin_face_idx,
                        "centroid": pin_centroids[0]
                        if pin_centroids
                        else [0.0, 0.0, 0.0],
                        "metadata": {},
                    }
                )

            # Escape holes (enclosed volumes without drainage)
            if rules.escape_hole_diameter_mm is not None:
                esc_has_risk, esc_centroids, esc_face_idx = detect_escape_hole_risk(
                    mesh, rules.escape_hole_diameter_mm
                )
                if esc_has_risk:
                    issues.append(
                        {
                            "category": "escape_hole",
                            "severity": "error",
                            "title": "Missing Escape Holes",
                            "description": (
                                f"Enclosed volume(s) detected without drainage holes"
                                f" ≥ {rules.escape_hole_diameter_mm}mm"
                                f" (required for {process_code} powder/resin evacuation)."
                            ),
                            "value": 0.0,
                            "threshold": float(rules.escape_hole_diameter_mm),
                            "faceIndices": esc_face_idx,
                            "centroid": esc_centroids[0]
                            if esc_centroids
                            else [0.0, 0.0, 0.0],
                            "metadata": {},
                        }
                    )

            # Connecting clearance (multi-body assemblies)
            if rules.connecting_clearance_mm is not None:
                cl_count, cl_centroids, cl_face_idx = detect_connecting_clearance(
                    mesh, rules.connecting_clearance_mm
                )
                if cl_count > 0:
                    issues.append(
                        {
                            "category": "clearance",
                            "severity": "warning",
                            "title": f"Insufficient Clearance ({cl_count} body pairs)",
                            "description": (
                                f"{cl_count} pair(s) of bodies have clearance below"
                                f" {rules.connecting_clearance_mm}mm for {process_code}."
                            ),
                            "value": float(cl_count),
                            "threshold": float(rules.connecting_clearance_mm),
                            "faceIndices": cl_face_idx,
                            "centroid": cl_centroids[0]
                            if cl_centroids
                            else [0.0, 0.0, 0.0],
                            "metadata": {},
                        }
                    )

            # Embossed / engraved features
            emb_count, emb_centroids, emb_face_idx = detect_embossed_engraved(
                mesh, rules.embossed_width_mm, rules.embossed_height_mm
            )
            if emb_count > 0:
                issues.append(
                    {
                        "category": "small_feature",
                        "severity": "warning",
                        "title": f"Small Embossed/Engraved Details ({emb_count})",
                        "description": (
                            f"{emb_count} raised/recessed detail(s) below minimum size"
                            f" (w≥{rules.embossed_width_mm}mm, h≥{rules.embossed_height_mm}mm)"
                            f" for {process_code}."
                        ),
                        "value": float(emb_count),
                        "threshold": float(rules.embossed_width_mm),
                        "faceIndices": emb_face_idx,
                        "centroid": emb_centroids[0]
                        if emb_centroids
                        else [0.0, 0.0, 0.0],
                        "metadata": {
                            "minWidthMm": rules.embossed_width_mm,
                            "minHeightMm": rules.embossed_height_mm,
                        },
                    }
                )

            report: dict[str, Any] = {
                "reportType": process_code,
                "issues": issues,
                # Legacy summary fields (backward compat with existing C# consumers)
                "thinWallCount": tw_count,
                "thinWallRegions": tw_centroids[:100],
                "overhangFaceCount": oh_count,
                "overhangAreaCm2": oh_area_cm2,
                "overhangRegions": oh_centroids[:100],
                "supportRequired": oh_count > 0 or tw_count > 0 or uw_count > 0,
                "estimatedSupportVolumeCm3": (
                    support_mm3 / 1000.0 if oh_count > 0 else None
                ),
                "smallDetailCount": sf_count,
            }
            if process_code in ("SLA", "SLA_DLP"):
                report["resinTrappingRisk"] = len(hollow_centroids) > 0
                report["resinTrappingRegions"] = hollow_centroids
                report["suctionRisk"] = oh_count > 0 and len(mesh.faces) > 1000
                report["suctionRegions"] = oh_centroids[:10]
                report["hollowRegions"] = hollow_centroids

            reports[process_code] = report

        # ── CNC Milling ───────────────────────────────────────────────────────
        mill_issues: list[dict[str, Any]] = []

        internal_radii = detect_internal_radii(mesh)
        small_radii = [
            r
            for r in internal_radii
            if 0.0 < r.radius_mm < MILLING_RULES.min_internal_radius_mm
        ]
        if small_radii:
            mill_ir_face_idx: list[int] = []
            for r in small_radii:
                mill_ir_face_idx.extend(r.face_indices)
            min_r = min(r.radius_mm for r in small_radii)
            tool = get_tool_for_radius(min_r)
            tool_str = (
                f"Requires Ø{tool[0]}mm endmill."
                if tool
                else "No standard tool achieves this radius."
            )
            mill_issues.append(
                {
                    "category": "internal_radius",
                    "severity": "warning",
                    "title": f"Small Internal Radii ({len(small_radii)} corners)",
                    "description": (
                        f"{len(small_radii)} internal corner(s) with radius below"
                        f" {MILLING_RULES.min_internal_radius_mm}mm."
                        f" Smallest: R{min_r:.2f}mm. {tool_str}"
                    ),
                    "value": float(min_r),
                    "threshold": float(MILLING_RULES.min_internal_radius_mm),
                    "faceIndices": mill_ir_face_idx[:2000],
                    "centroid": small_radii[0].centroid,
                    "metadata": {"toolDiameterMm": tool[0] if tool else None},
                }
            )

        cavities = detect_cavities(mesh)
        dc_count, dc_centroids, dc_face_idx = detect_deep_narrow_cavities(cavities)
        if dc_count > 0:
            max_dr = max(
                (
                    c.depth_ratio
                    for c in cavities
                    if c.depth_ratio > MILLING_RULES.cavity_depth_ratio
                ),
                default=float(MILLING_RULES.cavity_depth_ratio),
            )
            mill_issues.append(
                {
                    "category": "cavity_depth",
                    "severity": "error" if max_dr > 8.0 else "warning",
                    "title": f"Deep Cavities ({dc_count})",
                    "description": (
                        f"{dc_count} cavity/cavities exceed the {MILLING_RULES.cavity_depth_ratio}:1"
                        f" depth/width limit. Worst: {max_dr:.1f}:1."
                    ),
                    "value": float(max_dr),
                    "threshold": float(MILLING_RULES.cavity_depth_ratio),
                    "faceIndices": dc_face_idx[:2000],
                    "centroid": dc_centroids[0] if dc_centroids else [0.0, 0.0, 0.0],
                    "metadata": {"maxDepthRatio": float(max_dr)},
                }
            )

        tool_access = detect_tool_access(mesh)
        if tool_access.minimum_axes > 3:
            mill_issues.append(
                {
                    "category": "tool_access",
                    "severity": "info",
                    "title": f"{tool_access.minimum_axes}-Axis Machining Required",
                    "description": tool_access.details,
                    "value": float(tool_access.minimum_axes),
                    "threshold": 3.0,
                    "faceIndices": tool_access.inaccessible_face_indices[:2000],
                    "centroid": [0.0, 0.0, 0.0],
                    "metadata": {
                        "inaccessibleFaces": tool_access.inaccessible_face_count,
                    },
                }
            )

        ch_count, ch_centroids, ch_face_idx = detect_chatter_risk(mesh)
        if ch_count > 0:
            mill_issues.append(
                {
                    "category": "chatter_risk",
                    "severity": "warning",
                    "title": f"Chatter Risk ({ch_count} faces)",
                    "description": (
                        f"{ch_count} large flat face(s) with thin support may vibrate"
                        f" during milling (chatter)."
                    ),
                    "value": float(ch_count),
                    "threshold": 1.0,
                    "faceIndices": ch_face_idx,
                    "centroid": ch_centroids[0] if ch_centroids else [0.0, 0.0, 0.0],
                    "metadata": {},
                }
            )

        sc_count, sc_centroids, sc_face_idx = compute_sharp_corner_analysis(mesh, 45.0)
        if sc_count > 0:
            mill_issues.append(
                {
                    "category": "sharp_corner",
                    "severity": "warning",
                    "title": f"Sharp Internal Corners ({sc_count})",
                    "description": (
                        f"{sc_count} sharp corner(s) may require EDM or are inaccessible"
                        f" to standard endmills."
                    ),
                    "value": float(sc_count),
                    "threshold": 45.0,
                    "faceIndices": sc_face_idx[:2000],
                    "centroid": sc_centroids[0] if sc_centroids else [0.0, 0.0, 0.0],
                    "metadata": {},
                }
            )

        cnc_holes = detect_holes_mesh(mesh, MILLING_RULES.min_hole_diameter_mm)
        deep_drill_holes = []
        for h in cnc_holes:
            if h.depth_mm > 0 and h.diameter_mm > 0:
                dr = h.depth_mm / h.diameter_mm
                if dr > MILLING_RULES.hole_depth_typical_ratio:
                    deep_drill_holes.append((h, dr))
        if deep_drill_holes:
            ddh_face_idx: list[int] = []
            for h, _ in deep_drill_holes:
                ddh_face_idx.extend(h.face_indices)
            worst_dr = max(r for _, r in deep_drill_holes)
            mill_issues.append(
                {
                    "category": "hole",
                    "severity": (
                        "error"
                        if worst_dr > MILLING_RULES.hole_depth_feasible_ratio
                        else "warning"
                    ),
                    "title": f"Deep Drill Holes ({len(deep_drill_holes)})",
                    "description": (
                        f"{len(deep_drill_holes)} hole(s) exceed the"
                        f" {MILLING_RULES.hole_depth_typical_ratio}× depth/diameter limit."
                        f" Deepest: {worst_dr:.1f}×."
                    ),
                    "value": float(worst_dr),
                    "threshold": float(MILLING_RULES.hole_depth_typical_ratio),
                    "faceIndices": ddh_face_idx[:2000],
                    "centroid": deep_drill_holes[0][0].center,
                    "metadata": {"maxDepthRatio": float(worst_dr)},
                }
            )

        reports["CNC_MILL"] = {
            "reportType": "CNC_MILL",
            "issues": mill_issues,
            # Legacy CNC summary fields (backward compat)
            "sharpCornerCount": sc_count,
            "sharpCornerRegions": sc_centroids[:50],
            "hasUndercuts": False,
            "undercutRegions": [],
            "hasDrillHoles": len(cnc_holes) > 0,
            "drillHoleCount": len(cnc_holes),
            "requiresEdm": sc_count > 20,
            "requiresGrinding": False,
            "minimumFeatureSizeMm": MILLING_RULES.min_internal_radius_mm * 2.0,
            "internalRadiusIssues": len(small_radii),
            "cavityDepthIssues": dc_count,
            "toolAccessAxes": tool_access.minimum_axes,
            "chatterRiskCount": ch_count,
        }
        # Legacy "CNC" key — same data as CNC_MILL
        reports["CNC"] = dict(reports["CNC_MILL"])
        reports["CNC"]["reportType"] = "CNC"

        # ── CNC Turning ───────────────────────────────────────────────────────
        turn_issues: list[dict[str, Any]] = []
        axis_report = detect_axial_symmetry(mesh)

        if not axis_report.is_turnable:
            turn_issues.append(
                {
                    "category": "not_turnable",
                    "severity": "error",
                    "title": "Part Not Suitable for Turning",
                    "description": (
                        f"Symmetry deviation {axis_report.symmetry_deviation:.3f} exceeds"
                        f" turning threshold. Part likely requires milling."
                    ),
                    "value": float(axis_report.symmetry_deviation),
                    "threshold": 0.15,
                    "faceIndices": [],
                    "centroid": [0.0, 0.0, 0.0],
                    "metadata": {},
                }
            )
        else:
            ld_ratio = axis_report.length_diameter_ratio or 0.0
            if ld_ratio > TURNING_RULES.max_length_diameter_ratio:
                turn_issues.append(
                    {
                        "category": "ld_ratio",
                        "severity": "warning",
                        "title": f"High L/D Ratio ({ld_ratio:.1f}:1)",
                        "description": (
                            f"Length/diameter ratio {ld_ratio:.1f} exceeds the recommended"
                            f" {TURNING_RULES.max_length_diameter_ratio}:1 limit."
                            f" Steady rest or tailstock support required."
                        ),
                        "value": float(ld_ratio),
                        "threshold": float(TURNING_RULES.max_length_diameter_ratio),
                        "faceIndices": [],
                        "centroid": [0.0, 0.0, 0.0],
                        "metadata": {},
                    }
                )

            grooves = detect_grooves(mesh)
            narrow_grooves = [
                g for g in grooves if g.width_mm < TURNING_RULES.min_groove_width_mm
            ]
            if narrow_grooves:
                ng_face_idx: list[int] = []
                for g in narrow_grooves:
                    ng_face_idx.extend(g.face_indices)
                min_gw = min(g.width_mm for g in narrow_grooves)
                turn_issues.append(
                    {
                        "category": "groove",
                        "severity": "warning",
                        "title": f"Narrow Grooves ({len(narrow_grooves)})",
                        "description": (
                            f"{len(narrow_grooves)} groove(s) narrower than"
                            f" {TURNING_RULES.min_groove_width_mm}mm minimum grooving"
                            f" tool width. Narrowest: {min_gw:.2f}mm."
                        ),
                        "value": float(min_gw),
                        "threshold": float(TURNING_RULES.min_groove_width_mm),
                        "faceIndices": ng_face_idx[:2000],
                        "centroid": narrow_grooves[0].centroid,
                        "metadata": {"grooveCount": len(grooves)},
                    }
                )

        reports["CNC_TURN"] = {
            "reportType": "CNC_TURN",
            "issues": turn_issues,
            "isTurnable": axis_report.is_turnable,
            "primaryAxis": axis_report.primary_axis,
            "lengthDiameterRatio": axis_report.length_diameter_ratio,
            "symmetryDeviation": float(axis_report.symmetry_deviation),
        }

        return reports

    except Exception as e:
        logger.warning("_compute_dfm_worker failed: %s", e)
        return {}


def _generate_overlays_worker(
    stl_bytes: bytes,
    reports: dict[str, Any],
) -> dict[str, bytes]:
    """Phase 3 worker: generate overlay GLB bytes for each process+category.

    Returns a dict keyed by ``"{PROCESS}__{category}"`` (double-underscore avoids
    collisions with process codes like ``CNC_MILL``) mapping to GLB bytes.

    Runs in a separate process via ProcessPoolExecutor.

    Args:
        stl_bytes:  Tessellated mesh in STL format (Z-up, mm).
        reports:    DFM reports dict from ``_compute_dfm_worker``.
    """
    try:
        from core.overlay_generator import generate_multi_body_overlay_glb, generate_overlay_glb
    except ImportError:
        return {}

    result: dict[str, bytes] = {}

    # The viewer GLB is always in mm for all formats — _export_glb_worker converts
    # cascadio's meter-scale output to mm before upload. No unit conversion needed here.

    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.vertices) == 0:
            return result

        center = mesh.center_mass

        # Multi-body overlay: split the mesh into connected components and
        # tint each body a distinct colour so the user can see the separation.
        bodies = mesh.split(only_watertight=False)
        if len(bodies) > 1:
            try:
                multi_body_glb = generate_multi_body_overlay_glb(bodies, center)
                if multi_body_glb:
                    result["GENERAL__multi_body"] = multi_body_glb
            except Exception as _mb_err:
                logger.debug("Multi-body overlay GLB failed: %s", _mb_err)

        for process_code, report in reports.items():
            if process_code == "CNC_TURN":
                continue  # CNC turning overlays not yet supported
            issues = report.get("issues", [])
            seen_keys: set[str] = set()
            for issue in issues:
                category: str = issue.get("category", "")
                face_indices: list[int] = issue.get("faceIndices", [])
                if not face_indices or not category:
                    continue
                key = f"{process_code}__{category}"
                if key in seen_keys:
                    continue  # one overlay per process+category
                seen_keys.add(key)

                severity_per_face: dict[int, float] | None = None
                if category == "thin_wall":
                    # Uniform mid-severity gradient (exact per-face values require
                    # ray-cast thickness not yet available here).
                    severity_per_face = {fi: 0.5 for fi in face_indices}

                try:
                    glb = generate_overlay_glb(
                        mesh,
                        face_indices,
                        category,
                        center,
                        severity_per_face,
                    )
                    if glb:
                        result[key] = glb
                except Exception as _glb_err:
                    logger.debug(
                        "Overlay GLB failed for %s/%s: %s",
                        process_code,
                        category,
                        _glb_err,
                    )

    except Exception as exc:
        logger.warning("_generate_overlays_worker failed: %s", exc)

    return result


def _render_large_preview_worker(stl_bytes: bytes) -> dict[str, bytes | None]:
    """Phase 2 worker: generates 7 preview images (6 ortho + 1 iso) + 1200px ISO. Runs in a separate process."""
    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            return {}
        if len(mesh.faces) == 0:
            logger.warning("Mesh has no polygon faces — skipping preview generation")
            return {}
        return _generate_preview_images_sync(mesh)
    except Exception as e:
        logger.warning(f"_render_large_preview_worker failed: {e}")
        return {}


class GeometryProcessor:
    def __init__(self) -> None:
        self.executor = ProcessPoolExecutor(max_workers=max(4, os.cpu_count() or 4))

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)

    def analyze_stream(
        self, file_stream: io.BytesIO, file_extension: str
    ) -> tuple[GeometryMetrics, bytes | None, bytes | None]:
        file_stream.seek(0)
        data = file_stream.read()
        return self.analyze_bytes(data, file_extension)

    def analyze_bytes(
        self, data: bytes, file_extension: str
    ) -> tuple[GeometryMetrics, bytes | None, bytes | None]:
        file_stream = io.BytesIO(data)
        tmp_path = None
        try:
            file_stream.seek(0)
            ext = file_extension.strip(".").lower()

            mesh = None
            if ext in ["igs", "iges", "step", "stp"]:
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=f".{ext}", delete=False
                    ) as tmp:
                        shutil.copyfileobj(file_stream, tmp)
                        tmp_path = tmp.name

                    try:
                        gmsh.initialize()
                        gmsh.option.setNumber("General.Verbosity", 0)
                        gmsh.open(tmp_path)
                        # Use finer mesh for Phase 2 GLB quality (0.05mm) but with geometry healing
                        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", 0.05)
                        gmsh.option.setNumber("Mesh.AngleSmoothNormals", 0.30)
                        # Enable geometry healing for problematic STEP files
                        gmsh.option.setNumber("Geometry.OCCFixDegenerated", 1)
                        gmsh.option.setNumber("Geometry.OCCFixSmallEdges", 1)
                        gmsh.option.setNumber("Geometry.OCCFixSmallFaces", 1)
                        gmsh.option.setNumber("Geometry.OCCSewFaces", 1)
                        gmsh.model.mesh.generate(2)

                        _, coords, _ = gmsh.model.mesh.getNodes()
                        v = np.array(coords).reshape((-1, 3))

                        _, _, node_tags = gmsh.model.mesh.getElements(2)

                        if len(node_tags) > 0:
                            f = np.array(node_tags[0]) - 1
                            mesh = trimesh.Trimesh(vertices=v, faces=f.reshape((-1, 3)))
                        else:
                            raise ValueError("GMSH_TESSELLATION_FAILED: No triangles")
                    finally:
                        gmsh.finalize()
                except ValueError:
                    raise
                except Exception as e:
                    raise ValueError(f"CAD_LOAD_ERROR: {ext} ({str(e)})") from e
            else:
                mesh_data = trimesh.load(file_stream, file_type=ext, force="mesh")
                if isinstance(mesh_data, trimesh.Scene):
                    if not mesh_data.geometry:
                        raise ValueError("EMPTY_FILE_ERROR")
                    dumped_mesh2 = mesh_data.dump(concatenate=True)
                    if not isinstance(dumped_mesh2, trimesh.Trimesh) or len(dumped_mesh2.vertices) == 0:
                        raise ValueError("EMPTY_FILE_ERROR")
                    mesh = dumped_mesh2
                else:
                    mesh = cast(trimesh.Trimesh, mesh_data)

            if mesh is None or not isinstance(mesh, trimesh.Trimesh):
                raise ValueError("FILE_CORRUPT")

            is_manifold = bool(mesh.is_watertight)

            if is_manifold:
                volume_mm3 = float(mesh.volume)
                area_mm2 = float(mesh.area)
            else:
                try:
                    hull = mesh.convex_hull
                    volume_mm3 = float(hull.volume)
                    area_mm2 = float(hull.area)
                except Exception:
                    volume_mm3 = 0.0
                    area_mm2 = float(mesh.area)

            euler_number = int(mesh.euler_number)
            extents = mesh.extents
            if extents is None:
                bbox = BoundingBox(x=0.0, y=0.0, z=0.0)
                vol_bbox = 0.0
            else:
                bbox = BoundingBox(
                    x=float(extents[0]), y=float(extents[1]), z=float(extents[2])
                )
                vol_bbox = float(extents[0]) * float(extents[1]) * float(extents[2])

            support_mm3 = max(0.0, vol_bbox - volume_mm3)

            metrics = GeometryMetrics(
                volumeCm3=volume_mm3 / 1000.0,
                supportVolumeCm3=support_mm3 / 1000.0,
                surfaceAreaCm2=area_mm2 / 100.0,
                boundingBox=bbox,
                isManifold=is_manifold,
                triangleCount=len(mesh.faces),
                eulerNumber=euler_number,
            )

            glb_bytes = self._generate_glb(mesh)
            thumbnail_bytes = self._generate_thumbnail(mesh)

            return metrics, glb_bytes, thumbnail_bytes

        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"FILE_CORRUPT: {str(e)}") from e
        finally:
            if tmp_path and Path(tmp_path).exists():
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()

    def _generate_glb(self, mesh: trimesh.Trimesh) -> bytes | None:
        try:
            return cast(bytes, mesh.export(file_type="glb"))
        except Exception:
            return None

    def _generate_thumbnail(self, mesh: trimesh.Trimesh) -> bytes | None:
        if len(mesh.faces) == 0:
            return None
        try:
            preview_images = _generate_preview_images_sync(mesh)
            return preview_images.get("thumbnail_small")
        except Exception:
            return None

    async def analyze_async(
        self, file_stream: io.BytesIO, file_extension: str
    ) -> tuple[GeometryMetrics, bytes | None, bytes | None, dict[str, bytes | None]]:
        loop = asyncio.get_running_loop()
        file_stream.seek(0)
        data = file_stream.read()

        result = await loop.run_in_executor(
            self.executor, _analyze_bytes_worker, data, file_extension
        )

        metrics = GeometryMetrics(
            volumeCm3=result["volume_cm3"],
            supportVolumeCm3=result["support_volume_cm3"],
            surfaceAreaCm2=result["surface_area_cm2"],
            boundingBox=BoundingBox(**result["bounding_box"]),
            isManifold=result["is_manifold"],
            triangleCount=result["triangle_count"],
            eulerNumber=result["euler_number"],
        )

        return (
            metrics,
            result["glb_bytes"],
            result["thumbnail_bytes"],
            result.get("preview_images", {}),
        )
