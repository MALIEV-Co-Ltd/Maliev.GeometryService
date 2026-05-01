import asyncio
import concurrent.futures
import contextlib
import io
import logging
import math
import multiprocessing
import os
import shutil
import signal
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast


class PoolExecutorWrapper:
    """Wrapper for multiprocessing.Pool to provide ProcessPoolExecutor-compatible API.

    multiprocessing.Pool doesn't have submit()/map() methods but uses
    apply_async()/apply(). This wrapper provides the standard executor interface
    (submit, map, shutdown) for use with asyncio.run_in_executor().
    """

    def __init__(
        self,
        processes: int | None = None,
        maxtasksperchild: int | None = None,
        initializer: Callable[[], None] | None = None,
    ) -> None:
        self.maxtasksperchild = maxtasksperchild  # exposed for test introspection
        self._pool = multiprocessing.Pool(
            processes=processes,
            maxtasksperchild=maxtasksperchild,
            initializer=initializer,
        )

    def submit(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future:
        """Submit a function to be executed asynchronously.

        Wraps Pool.apply_async() to provide ProcessPoolExecutor-compatible API.
        Returns a concurrent.futures.Future compatible wrapper.
        """
        import threading

        actual_args = args

        kwds = kwargs if kwargs is not None else {}
        result = self._pool.apply_async(fn, actual_args, kwds=kwds)

        future = concurrent.futures.Future()

        def get_result() -> None:
            try:
                future.set_result(result.get())
            except Exception as e:
                future.set_exception(e)

        thread = threading.Thread(target=get_result, daemon=True)
        thread.start()

        return future

    def map(self, fn: Callable[..., Any], *iterables: Any, **kwargs: Any):
        """Map function over iterables asynchronously.

        Uses Pool.imap_unordered() for async mapping.
        """
        chunksize = kwargs.get("chunksize", 1)
        return self._pool.imap_unordered(fn, iterables, chunksize=chunksize)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:  # noqa: ARG002
        """Shutdown the executor pool.

        Uses Pool.close() + Pool.join() for graceful shutdown.
        """
        self._pool.close()
        if wait:
            self._pool.join()

    def __enter__(self) -> "PoolExecutorWrapper":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.shutdown(wait=True)


import gmsh  # noqa: E402
import numpy as np  # noqa: E402
import trimesh  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from src.core.config import settings  # noqa: E402

logger = logging.getLogger(__name__)

THIN_WALL_THRESHOLD_MM = 0.8
OVERHANG_ANGLE_THRESHOLD_DEGREES = 45.0

# ---------------------------------------------------------------------------
# GLB Cache - Reduce redundant file I/O for thumbnail/preview/export workers
#
# The same GLB file (123MB+ for large assemblies) was being loaded 3+ times:
# - Once for thumbnail generation
# - Once for preview generation
# - Once for GLB export
#
# This cache stores loaded trimesh objects in memory to avoid repeated disk I/O.
# Key: GLB file path, Value: (trimesh object, load timestamp)
# ---------------------------------------------------------------------------
_glb_cache: dict[str, tuple[Any, float]] = {}
_GLB_CACHE_MAX_SIZE = 3  # Keep at most 3 GLBs in memory (~369MB for 3x 123MB files)
_GLB_CACHE_TTL_SECONDS = 300  # Cache expires after 5 minutes


def _get_cached_glb(glb_path: str) -> Any | None:
    """Get a GLB from cache, or load and cache it if not present.

    Returns the trimesh object (Scene or Trimesh) or None if loading fails.
    """
    import time

    # Check cache
    if glb_path in _glb_cache:
        cached_obj, timestamp = _glb_cache[glb_path]
        age = time.time() - timestamp

        if age < _GLB_CACHE_TTL_SECONDS:
            obj_type = "Scene" if isinstance(cached_obj, trimesh.Scene) else "Trimesh"
            geom_count = (
                len(cached_obj.geometry) if isinstance(cached_obj, trimesh.Scene) else 1
            )
            logger.debug(
                "GLB cache hit: %s (%.1fs old, type=%s, geometries=%d)",
                glb_path,
                age,
                obj_type,
                geom_count,
            )
            return cached_obj
        # Expired - remove from cache
        logger.debug("GLB cache expired: %s", glb_path)
        del _glb_cache[glb_path]

    # Load from disk
    try:
        with open(glb_path, "rb") as fh:  # noqa: PTH123
            glb_bytes = fh.read()

        logger.info("Loading GLB for caching: %s (%d bytes)", glb_path, len(glb_bytes))
        scene_data = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")

        # Log what we loaded
        if isinstance(scene_data, trimesh.Scene):
            geom_count = len(scene_data.geometry)
            logger.info(f"Loaded GLB Scene with {geom_count} geometries")
        else:
            logger.info("Loaded single Trimesh object")

        # Add to cache (with LRU eviction if needed)
        if len(_glb_cache) >= _GLB_CACHE_MAX_SIZE:
            # Remove oldest entry (first key)
            oldest_key = next(iter(_glb_cache))
            logger.debug("GLB cache full, evicting: %s", oldest_key)
            del _glb_cache[oldest_key]

        _glb_cache[glb_path] = (scene_data, time.time())
        logger.info(
            "GLB cached: %s (cache size: %d/%d)",
            glb_path,
            len(_glb_cache),
            _GLB_CACHE_MAX_SIZE,
        )

        return scene_data

    except Exception as e:
        logger.warning(
            "Failed to load GLB for caching: %s - %s", glb_path, e, exc_info=True
        )
        return None


def _clear_glb_cache() -> None:
    """Clear the GLB cache (useful for testing or memory pressure)."""
    global _glb_cache
    cleared = len(_glb_cache)
    _glb_cache.clear()
    logger.info("GLB cache cleared: %d entries", cleared)


def _pid_probe_worker() -> int:
    """Return the PID of the current worker process.

    Module-level so it is picklable by multiprocessing.Pool / ProcessPoolExecutor.
    Used by tests to verify worker recycling.
    """
    return os.getpid()


# ---------------------------------------------------------------------------
# OCC tessellation cache — avoids re-tessellating the same STEP/IGES file when
# the user switches between FDM / SLA / CNC.
#
# Key: "<sha256_prefix>:<tolerance_bucket>"
# Value: (features, face_tag_to_tri, cached_at)
#
# Lives in each process's address space. With max_workers=1 on dfm_executor
# and max_tasks_per_child=5, one worker handles up to 5 jobs (FDM/SLA/CNC
# clicks) before being recycled — enough to cover a typical user session.
# In the main process (API thread pool), the cache is shared across threads.
# ---------------------------------------------------------------------------
_occ_cache: dict[str, tuple[list, dict, float]] = {}
_OCC_CACHE_MAX = 6  # retain last 6 (file × process-bucket) pairs
_OCC_CACHE_TTL = 600  # seconds

# ---------------------------------------------------------------------------
# Pre-computed mesh data cache — avoids re-running detect_hollow_regions and
# detect_holes_mesh for each process click on the same file.
#
# Key: sha256 prefix of stl_bytes
# Value: {"hollow_centroids": ..., "all_holes": ..., "cached_at": float}
# ---------------------------------------------------------------------------
_mesh_precompute_cache: dict[str, dict] = {}
_MESH_PRECOMPUTE_CACHE_MAX = 3  # keep small — each entry holds numpy arrays per body


def _occ_cache_key(cad_bytes: bytes, tolerance: float) -> str:
    import hashlib

    sha = hashlib.sha256(cad_bytes).hexdigest()[:16]
    # Bucket tolerance to 2dp so numerically close values share the same key
    return f"{sha}:{tolerance:.2f}"


def _mesh_cache_key(stl_bytes: bytes) -> str:
    import hashlib

    return hashlib.sha256(stl_bytes[:1_048_576]).hexdigest()[:16]


# ---------------------------------------------------------------------------
# DFM worker initializer — called once per worker spawn to pre-import heavy
# modules so the first real DFM job doesn't pay the cold-start import cost.
# ---------------------------------------------------------------------------
def _dfm_worker_initializer() -> None:
    """Pre-import DFM analysis modules in the worker process.

    Called once via ProcessPoolExecutor(initializer=_dfm_worker_initializer).
    Amortises the ~1-3 s cold-start cost across max_tasks_per_child jobs.
    """
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        import trimesh  # noqa: F401

        from src.core.cnc_analyzers import (  # noqa: F401
            compute_sharp_corner_analysis,
            detect_cavities,
            detect_chatter_risk,
        )
        from src.core.dfm_thresholds import MILLING_RULES, PRINTING_RULES  # noqa: F401
        from src.core.mesh_analyzers import (  # noqa: F401
            compute_overhang_analysis,
            compute_thin_wall_analysis,
            detect_holes_mesh,
            detect_hollow_regions,
        )

        # OCC is optional — don't fail if unavailable
        try:  # noqa: SIM105
            from OCC.Core.BRep import BRep_Tool  # noqa: F401
        except ImportError:
            pass
        logger.info("DFM worker pre-imports complete (pid=%d)", os.getpid())
    except Exception as exc:
        logger.warning("DFM worker pre-import failed (non-fatal): %s", exc)


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
    non_manifold_reason: str | None = Field(default=None, alias="nonManifoldReason")
    non_manifold_face_count: int | None = Field(
        default=None, alias="nonManifoldFaceCount"
    )


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
    thin_wall_count: int = Field(default=0, alias="thinWallCount")
    thin_wall_regions: list[list[float]] = Field(
        default_factory=list, alias="thinWallRegions"
    )
    overhang_face_count: int = Field(default=0, alias="overhangFaceCount")
    overhang_area_cm2: float = Field(default=0.0, alias="overhangAreaCm2")
    overhang_regions: list[list[float]] = Field(
        default_factory=list, alias="overhangRegions"
    )
    resin_trapping_risk: bool = Field(default=False, alias="resinTrappingRisk")
    resin_trapping_regions: list[list[float]] = Field(
        default_factory=list, alias="resinTrappingRegions"
    )
    suction_risk: bool = Field(default=False, alias="suctionRisk")
    suction_regions: list[list[float]] = Field(
        default_factory=list, alias="suctionRegions"
    )
    hollow_regions: list[list[float]] = Field(
        default_factory=list, alias="hollowRegions"
    )
    issues: list[DfmIssueItem] = Field(default_factory=list)


class CncDfmReport(BaseModel):
    """DFM analysis results specific to CNC machining."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    report_type: str = Field(default="CNC", alias="reportType")
    sharp_corner_count: int = Field(default=0, alias="sharpCornerCount")
    sharp_corner_regions: list[list[float]] = Field(
        default_factory=list, alias="sharpCornerRegions"
    )
    has_undercuts: bool = Field(default=False, alias="hasUndercuts")
    undercut_regions: list[list[float]] = Field(
        default_factory=list, alias="undercutRegions"
    )
    has_drill_holes: bool = Field(default=False, alias="hasDrillHoles")
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
    """  # noqa: E501
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
        SHARP_CORNER_THRESHOLD_DEGREES = 45.0  # noqa: N806
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

        if isinstance(split, trimesh.Scene) and len(split.geometry) > 1:
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

    # Initialize virtual framebuffer for headless rendering in Docker (Linux only)
    if hasattr(pv, "start_xvfb"):
        with contextlib.suppress(Exception):
            pv.start_xvfb()

    # 85mm lens equivalent: vertical FOV = 2*atan(24/(2*85)) ≈ 16°
    view_angle = 16.0

    # Compute distance so the model's bounding sphere fills the view with padding.
    # half_fov = 8°, so distance = (max_dim/2) / tan(8°) * padding
    distance = (
        (max_dim / 2.0) / math.tan(math.radians(view_angle / 2.0)) * distance_padding
    )

    pl = pv.Plotter(off_screen=True, window_size=[size, size], lighting=None)
    pl.set_background(None)  # Transparent for light/dark mode theme overlay
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


def _prepare_mesh_for_rendering(mesh: trimesh.Trimesh) -> dict[str, Any] | None:
    """
    Prepare PyVista mesh data for parallel rendering.

    Extracts feature edges, computes normals, and calculates camera parameters.
    Returns a dict with serialized data that can be passed to rendering workers.

    Args:
        mesh: Trimesh object to prepare

    Returns:
        Dict with keys: pv_mesh, feature_edges, center, max_dim
        Returns None on error
    """
    try:
        import numpy as np
        import pyvista as pv

        pv.OFF_SCREEN = True
        # pyvista 0.44+ uses lazy module loading: pv.Plotter is only resolved from
        # pyvista.plotting on first access.  If 8 ThreadPoolExecutor threads all
        # reach `pv.Plotter(...)` simultaneously they race through that lazy import
        # and some threads get AttributeError("module 'pyvista' has no attribute
        # 'Plotter'").  Force the import here (single-threaded) so it's cached before
        # any threads start.
        _ = pv.Plotter  # noqa: F841
    except Exception as e:
        logger.error(f"Failed to import pyvista: {e}")
        return None

    try:
        if len(mesh.faces) == 0:
            logger.warning("Mesh has no polygon faces — skipping preview generation")
            return None

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

        return {
            "pv_mesh": pv_mesh,
            "feature_edges": feature_edges,
            "center": center,
            "max_dim": max_dim,
        }

    except Exception as e:
        logger.error(f"Failed to prepare mesh for rendering: {e}")
        return None


def _render_single_view_from_prepared_mesh(
    mesh_data: dict[str, Any],
    view_name: str,
    size: int = DEFAULT_SIZE,
) -> tuple[str, bytes | None]:
    """
    Render a single view using pre-prepared mesh data.

    Args:
        mesh_data: Dict from _prepare_mesh_for_rendering with pv_mesh, feature_edges, center, max_dim
        view_name: Name of view to render (from ALL_VIEWS or "thumbnail_large")
        size: Image size in pixels

    Returns:
        Tuple of (result_key, image_bytes) where result_key is like "front_small" or "thumbnail_large"
        Returns (result_key, None) on failure
    """  # noqa: E501
    try:
        # Get view configuration
        if view_name == "thumbnail_large":
            config = VIEW_CONFIGS["iso"]
            result_key = "thumbnail_large"
        else:
            config = VIEW_CONFIGS[view_name]
            result_key = (
                "thumbnail_small" if view_name == "iso" else f"{view_name}_small"
            )

        image_bytes = _render_single_view(
            pv_mesh=mesh_data["pv_mesh"],
            feature_edges=mesh_data["feature_edges"],
            center=mesh_data["center"],
            max_dim=mesh_data["max_dim"],
            camera_dir=config["camera_dir"],
            viewup=config["viewup"],
            size=size,
            distance_padding=config["distance_padding"],
            fmt="WEBP",
            quality=80,
        )

        return (result_key, image_bytes)

    except Exception as e:
        logger.error(f"Failed to render view {view_name}: {e}")
        result_key = "thumbnail_small" if view_name == "iso" else f"{view_name}_small"
        if view_name == "thumbnail_large":
            result_key = "thumbnail_large"
        return (result_key, None)


def _generate_preview_images_parallel(mesh: trimesh.Trimesh) -> dict[str, bytes | None]:
    """
    Generate preview images in PARALLEL using ThreadPoolExecutor.

    All 8 images (6 orthographic + 1 isometric small + 1 isometric large) render concurrently.

    Speedup: ~8x faster than sequential rendering.
    """  # noqa: E501
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start_time = time.time()
    results: dict[str, bytes | None] = {}
    empty: dict[str, bytes | None] = {f"{view}_small": None for view in ORTHO_VIEWS}
    empty["thumbnail_small"] = None
    empty["thumbnail_large"] = None

    logger.info("Starting PARALLEL preview generation for 8 views")

    # Prepare mesh once
    mesh_data = _prepare_mesh_for_rendering(mesh)
    if mesh_data is None:
        return empty

    prep_time = time.time() - start_time
    logger.info(f"Mesh prepared for rendering in {prep_time:.2f}s")

    # Define all views to render
    views_to_render = [
        (view_name, DEFAULT_SIZE)
        for view_name in ALL_VIEWS  # 7 views at 256px
    ] + [("thumbnail_large", 1200)]  # 1 view at 1200px

    # Render all views in parallel
    render_start = time.time()
    completed_count = 0

    preview_workers = max(1, min(settings.GEOMETRY_PREVIEW_RENDER_WORKERS, 8))
    logger.info(
        "Rendering %d views with %d preview workers",
        len(views_to_render),
        preview_workers,
    )
    with ThreadPoolExecutor(max_workers=preview_workers) as executor:
        # Submit all rendering tasks
        futures = {
            executor.submit(
                _render_single_view_from_prepared_mesh, mesh_data, view_name, size
            ): view_name
            for view_name, size in views_to_render
        }

        # Collect results as they complete
        for future in as_completed(futures):
            view_name = futures[future]
            try:
                result_key, image_bytes = future.result(
                    timeout=30
                )  # 30s timeout per view
                results[result_key] = image_bytes
                completed_count += 1
                elapsed = time.time() - render_start
                logger.debug(
                    f"[{completed_count}/{len(views_to_render)}] Rendered {view_name} -> {result_key} "  # noqa: E501
                    f"in {elapsed:.2f}s total"
                )
            except Exception as e:
                logger.error(f"Failed to render view {view_name}: {e}")
                # Set failed view to None
                if view_name == "thumbnail_large":
                    results["thumbnail_large"] = None
                elif view_name == "iso":
                    results["thumbnail_small"] = None
                else:
                    results[f"{view_name}_small"] = None

    total_time = time.time() - start_time
    successful = sum(1 for v in results.values() if v is not None)

    logger.info(
        f"PARALLEL preview generation complete: {successful}/{len(views_to_render)} views "  # noqa: E501
        f"in {total_time:.2f}s (~{total_time / len(views_to_render):.2f}s per view average)"  # noqa: E501
    )

    # Ensure all keys exist
    for key in empty:
        if key not in results:
            results[key] = None

    return results


def _generate_preview_images_sync(mesh: trimesh.Trimesh) -> dict[str, bytes | None]:
    """
    Generate 7 preview images (6 orthographic + 1 isometric) as WebP SEQUENTIALLY.

    Uses VTK/OSMesa for headless CPU rendering (no GPU required).

    NOTE: This is the LEGACY sequential version. Use _generate_preview_images_parallel
    for ~8x faster parallel rendering.
    """
    import time

    start_time = time.time()
    logger.info("Starting SEQUENTIAL preview generation (legacy mode)")

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

        view_count = 0
        for view_name in ALL_VIEWS:
            config = VIEW_CONFIGS[view_name]
            result_key = (
                "thumbnail_small" if view_name == "iso" else f"{view_name}_small"
            )
            try:
                view_start = time.time()
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
                view_count += 1
                view_time = time.time() - view_start
                logger.debug(f"Rendered view {view_name} in {view_time:.2f}s")
            except Exception as e:
                logger.error(
                    f"Failed to generate preview image for view {view_name}: {e}"
                )
                results[result_key] = None

        # 1200px ISO WebP — hi-res fallback for the detail card viewer
        try:
            view_start = time.time()
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
            view_time = time.time() - view_start
            logger.debug(f"Rendered thumbnail_large in {view_time:.2f}s")
        except Exception as e:
            logger.error(f"Failed to generate 1200px ISO WebP preview: {e}")
            results["thumbnail_large"] = None

        total_time = time.time() - start_time
        successful = sum(1 for v in results.values() if v is not None)

        logger.info(
            f"SEQUENTIAL preview generation complete: {successful}/{len(ALL_VIEWS) + 1} views "  # noqa: E501
            f"in {total_time:.2f}s (~{total_time / max(successful, 1):.2f}s per view average)"  # noqa: E501
        )

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

    def _sigalarm_handler(signum, frame):  # noqa: ARG001
        nonlocal timeout_fired
        timeout_fired = True
        # Attempt graceful gmsh finalize before os._exit
        with contextlib.suppress(Exception):
            gmsh.finalize()
        # Log before dying so Aspire structured logs capture it
        logger.error(
            f"gmsh mesh generation timed out after {timeout_seconds}s — killing worker process",  # noqa: E501
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
            # Use 2mm max element to keep mesh lightweight for web viewer.
            # Tighter values (e.g. 0.1mm) produce enormous meshes on large parts
            # and are the primary cause of Phase 1 and DFM timeouts.
            gmsh.option.setNumber("Mesh.CharacteristicLengthMax", 2.0)
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
        # subprocess.  If the thread fires before gmsh returns, we terminate the process.  # noqa: E501
        result = {}

        def _gmsh_work():
            try:
                gmsh.initialize()
                gmsh.option.setNumber("General.Verbosity", 0)
                gmsh.open(file_path)
                # Use 2mm max element to keep mesh lightweight for web viewer.
                # Tighter values (e.g. 0.1mm) produce enormous meshes on large parts
                # and are the primary cause of Phase 1 and DFM timeouts.
                gmsh.option.setNumber("Mesh.CharacteristicLengthMax", 2.0)
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
                with contextlib.suppress(Exception):
                    gmsh.finalize()

        worker_thread = threading.Thread(target=_gmsh_work, daemon=True)
        worker_thread.start()
        worker_thread.join(timeout=timeout_seconds)

        if worker_thread.is_alive() or "error" in result:
            # gmsh is still running or raised an error — kill this process
            logger.error(
                f"gmsh mesh generation timed out after {timeout_seconds}s (Windows) — killing worker process",  # noqa: E501
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
) -> tuple[list[trimesh.Trimesh], bytes, int, list[str]]:
    """
    Load STEP/IGES file via cascadio (OpenCascade), preserving multi-body structure.

    This is a compatibility wrapper - calls the isolated version.
    """
    with open(file_path, "rb") as f:  # noqa: PTH123
        file_bytes = f.read()
    return _load_cad_with_cascadio_isolated(file_bytes, timeout_seconds)


def _cascadio_subprocess_load(
    file_path: str,
    glb_output_path: str,
    timeout_seconds: int = 60,  # noqa: ARG001
) -> int:
    """Load CAD via cascadio in isolated process.

    This runs in a separate process so we can terminate it on timeout
    and cleanly kill any stuck C-extension threads.

    Returns:
        int: cascadio return code (0 = success)

    Raises:
        TimeoutError: if loading exceeds timeout_seconds
    """
    import cascadio

    return cascadio.step_to_glb(
        file_path,
        glb_output_path,
        tol_linear=0.05,
        tol_angular=0.1,
    )


def _load_cad_with_cascadio_isolated(
    file_bytes: bytes,
    timeout_seconds: int = 60,
) -> tuple[list[trimesh.Trimesh], bytes, int, list[str]]:
    """Load CAD via cascadio in isolated process with proper timeout handling.

    Uses ProcessPoolExecutor with spawn context so we can terminate the process
    on timeout and cleanly kill any stuck C-extension threads.

    Returns:
        tuple[list[trimesh.Trimesh], bytes, int, list[str]]: meshes, glb_bytes, body_count, body_names
    """  # noqa: E501
    import concurrent.futures
    import multiprocessing
    import os
    import tempfile

    # Write input to temp file for subprocess
    with tempfile.NamedTemporaryFile(suffix=".stp", delete=False) as inp:
        inp.write(file_bytes)
        inp_path = inp.name

    glb_path: str | None = None
    pool: multiprocessing.context.BaseContext.Pool | None = None

    try:
        # Create temp output path
        with tempfile.NamedTemporaryFile(suffix=".glb", delete=False) as out:
            glb_path = out.name

        # Use spawn context for clean process isolation
        ctx = multiprocessing.get_context("spawn")
        pool = ctx.Pool(processes=1)

        # Submit to subprocess pool
        future = pool.apply_async(
            _cascadio_subprocess_load,
            (inp_path, glb_path, timeout_seconds),
        )

        try:
            ret = future.get(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            pool.terminate()
            pool.join()

            logger.warning(
                f"cascadio timed out after {timeout_seconds}s loading {inp_path} - "
                f"terminated process for clean cleanup"
            )

            raise TimeoutError(  # noqa: B904
                f"cascadio timed out after {timeout_seconds}s loading {inp_path}"
            )
        finally:
            pool.close()
            pool.join()

        if ret != 0:
            raise ValueError(f"cascadio.step_to_glb returned {ret}")

        # Re-parse GLB in main process (reuse existing logic)
        loaded = trimesh.load(glb_path)

        if isinstance(loaded, trimesh.Scene):
            body_names = list(loaded.geometry.keys())
            meshes = list(loaded.geometry.values())
            if not meshes:
                raise ValueError("cascadio produced empty scene")
            body_count = len(meshes)

            for mesh in meshes:
                mesh.merge_vertices(digits_vertex=3)
                trimesh.repair.fix_winding(mesh)
                mesh.apply_scale(1000.0)

            with open(glb_path, "rb") as f:  # noqa: PTH123
                glb_bytes = f.read()

            return meshes, glb_bytes, body_count, body_names

        if isinstance(loaded, trimesh.Trimesh):
            mesh = loaded
            if len(mesh.vertices) == 0:
                raise ValueError("cascadio produced empty mesh")
            mesh.merge_vertices(digits_vertex=3)
            trimesh.repair.fix_winding(mesh)
            mesh.apply_scale(1000.0)

            with open(glb_path, "rb") as f:  # noqa: PTH123
                glb_bytes = f.read()

            return [mesh], glb_bytes, 1, ["Body_01"]
        raise ValueError(f"cascadio produced unexpected type: {type(loaded)}")

    finally:
        # Cleanup temp files
        if inp_path and os.path.exists(inp_path):  # noqa: PTH110
            os.unlink(inp_path)  # noqa: PTH108
        if glb_path and os.path.exists(glb_path):  # noqa: PTH110
            os.unlink(glb_path)  # noqa: PTH108
        if pool:
            try:
                pool.terminate()
                pool.join()
            except Exception:
                pass


def _split_significant_mesh_bodies(mesh: trimesh.Trimesh) -> list[trimesh.Trimesh]:
    """Split mesh-native assemblies into bodies and ignore tiny face islands."""
    if len(mesh.faces) == 0:
        return []

    try:
        parts = mesh.split(only_watertight=False)
    except Exception:
        return [mesh]

    if not parts:
        return [mesh]

    significant = [part for part in parts if len(part.faces) >= 10]
    return significant or [mesh]


def _mesh_manifold_info(mesh: trimesh.Trimesh) -> tuple[bool, str | None, int | None]:
    """Returns (is_manifold, reason_str_or_None, broken_face_count_or_None)."""
    mesh = mesh.copy()
    mesh.merge_vertices()
    edge_counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)[1]
    if bool(np.all(edge_counts <= 2)) and bool(mesh.is_watertight):
        return True, None, None

    boundary_count = int(np.sum(edge_counts == 1))
    non_manifold_edge_count = int(np.sum(edge_counts > 2))
    try:
        broken = trimesh.repair.broken_faces(mesh, color=None)
        broken_count = int(len(broken)) if broken is not None else None
    except Exception:
        broken_count = None

    parts = []
    if boundary_count > 0:
        parts.append(f"{boundary_count} open boundary edge(s)")
    if non_manifold_edge_count > 0:
        parts.append(
            f"{non_manifold_edge_count} non-manifold edge(s) shared by >2 faces"
        )
    reason = "; ".join(parts) if parts else "mesh is not watertight"
    return False, reason, broken_count


def _export_multibody_scene_glb(
    meshes: list[trimesh.Trimesh],
    body_names: list[str],
) -> bytes | None:
    """Export mesh-native disconnected bodies as separate glTF scene nodes."""
    try:
        scene = trimesh.Scene()
        for index, mesh in enumerate(meshes):
            name = (
                body_names[index]
                if index < len(body_names)
                else f"Body_{index + 1:02d}"
            )
            scene.add_geometry(mesh, geom_name=name, node_name=name)
        return cast(bytes, scene.export(file_type="glb"))
    except Exception as exc:
        logger.warning("Failed to export multi-body scene GLB: %s", exc)
        return None


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

        mesh_list: list[trimesh.Trimesh] = []
        cad_glb_bytes: bytes | None = None
        cad_body_count: int = 1
        body_names: list[str] = []
        metrics_mesh_raw: trimesh.Trimesh | None = None
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
                mesh_list, cad_glb_bytes, cad_body_count, body_names = (
                    _load_cad_with_cascadio_isolated(
                        open(tmp_path, "rb").read(),  # noqa: SIM115, PTH123
                        timeout_seconds=60,
                    )
                )
                total_triangles = sum(len(m.faces) for m in mesh_list)
                logger.info(
                    "cascadio tessellation complete",
                    extra={
                        "event": "cascadio_complete",
                        "extension": ext,
                        "body_count": cad_body_count,
                        "triangle_count": total_triangles,
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
                # Preserve multi-body structure for non-CAD formats too
                for geom_name, geom in mesh_data.geometry.items():
                    if isinstance(geom, trimesh.Trimesh) and len(geom.vertices) > 0:
                        bodies = _split_significant_mesh_bodies(geom)
                        is_single_geom_body = len(bodies) == 1
                        for body in bodies:
                            mesh_list.append(body)
                            body_names.append(
                                str(geom_name)
                                if is_single_geom_body
                                else f"Body_{len(mesh_list):02d}"
                            )
                cad_body_count = len(mesh_list)
                if not mesh_list:
                    raise ValueError("EMPTY_FILE_ERROR")
                dumped_mesh = mesh_data.dump(concatenate=True)
                if (
                    isinstance(dumped_mesh, trimesh.Trimesh)
                    and len(dumped_mesh.vertices) > 0
                ):
                    metrics_mesh_raw = dumped_mesh
            else:
                mesh = cast(trimesh.Trimesh, mesh_data)
                metrics_mesh_raw = mesh
                mesh_list = _split_significant_mesh_bodies(mesh)
                cad_body_count = len(mesh_list)
                body_names = [f"Body_{i + 1:02d}" for i in range(cad_body_count)]
            # glTF/GLB spec mandates meters, but most CAD software exports GLBs in mm.
            # If all dimensions are < 1 unit the file is almost certainly in meters;
            # apply ×1000 so downstream metrics are in mm.  Otherwise assume mm already.
            if ext in ("glb", "gltf"):
                all_dims = []
                for m in mesh_list:
                    if hasattr(m, "extents") and m.extents is not None:
                        all_dims.extend(m.extents.tolist())
                if all_dims and max(all_dims) < 1.0:
                    for mesh in mesh_list:
                        mesh.apply_scale(1000.0)
                cad_glb_bytes = (
                    data  # uploaded GLB bytes stay in meters (correct for BabylonJS)
                )

        if not mesh_list:
            raise ValueError("FILE_CORRUPT: No valid meshes found")

        # Concatenate ONLY for aggregate metrics (volume, bbox, etc.)
        # Preserve mesh_list for Phase 2 multi-body handling
        if metrics_mesh_raw is not None:
            metrics_mesh = metrics_mesh_raw
        elif len(mesh_list) > 1:
            metrics_mesh = trimesh.util.concatenate(mesh_list)
        else:
            metrics_mesh = mesh_list[0]

        # Manifold check: parametric CAD formats (STEP/IGES/etc.) are guaranteed
        # valid closed solids when OCC loads them successfully, so skip the trimesh
        # edge-count check — it produces false positives due to tessellation
        # t-junctions and unmerged vertices at shared faces.
        # For mesh-native formats (.stl/.obj/.ply) run the check per-body after
        # merging coincident vertices to reduce artefact-driven false positives.
        _PARAMETRIC_EXTS = {"step", "stp", "iges", "igs", "x_t", "x_b", "sat", "brep"}  # noqa: N806

        if ext in _PARAMETRIC_EXTS:
            is_manifold = True
            non_manifold_reason: str | None = None
            non_manifold_face_count: int | None = None
        else:
            diagnostic_meshes = (
                [metrics_mesh_raw] if metrics_mesh_raw is not None else mesh_list
            )
            body_infos = [
                _mesh_manifold_info(m) for m in diagnostic_meshes if m is not None
            ]
            is_manifold = all(info[0] for info in body_infos)
            if not is_manifold:
                reasons = [info[1] for info in body_infos if info[1]]
                non_manifold_reason = (
                    "; ".join(reasons) if reasons else "mesh is not watertight"
                )
                counts = [info[2] for info in body_infos if info[2] is not None]
                non_manifold_face_count = sum(counts) if counts else None
            else:
                non_manifold_reason = None
                non_manifold_face_count = None

        if metrics_mesh.is_watertight:
            volume_mm3 = float(metrics_mesh.volume)
            area_mm2 = float(metrics_mesh.area)
        else:
            try:
                hull = metrics_mesh.convex_hull
                volume_mm3 = float(hull.volume)
                area_mm2 = float(hull.area)
            except Exception:
                volume_mm3 = 0.0
                area_mm2 = float(metrics_mesh.area)

        euler_number = int(metrics_mesh.euler_number)
        extents = metrics_mesh.extents
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

        # Export STL bytes PER BODY (preserve multi-body structure for Phase 3)
        mesh_stl_bytes_dict: dict[int, bytes] = {}
        for i, body_mesh in enumerate(mesh_list):
            try:
                stl_bytes = cast(bytes, body_mesh.export(file_type="stl"))
                mesh_stl_bytes_dict[i] = stl_bytes
            except Exception as ex:
                logger.warning(
                    "STL export failed for body %d, Phase 3 may produce degraded results: %s",  # noqa: E501
                    i,
                    ex,
                )
                # If original file was STL, use original bytes for this body
                if ext == "stl" and i == 0:
                    mesh_stl_bytes_dict[i] = data

        # Legacy single-body field for backward compatibility (use first body)
        mesh_stl_bytes = mesh_stl_bytes_dict.get(0) if mesh_stl_bytes_dict else None

        # Export GLB for ALL formats so Phase 2 has a uniform artifact.
        # STEP/IGES already have cad_glb_bytes from cascadio.
        # Uploaded GLBs use the original bytes.
        # STL/OBJ/3MF: export the processed mesh to GLB.
        if not cad_glb_bytes and ext not in ("step", "stp", "igs", "iges"):
            try:
                if len(mesh_list) > 1:
                    cad_glb_bytes = _export_multibody_scene_glb(mesh_list, body_names)
                else:
                    cad_glb_bytes = cast(bytes, metrics_mesh.export(file_type="glb"))
            except Exception as e:
                logger.warning(
                    f"Failed to export GLB from {ext} mesh, DFM may be degraded: {e}",
                    extra={
                        "event": "glb_export_failed",
                        "extension": ext,
                        "error": str(e),
                    },
                )
                cad_glb_bytes = None

        logger.info(
            "Metrics computed: %d bodies, volume=%.1fcm³, triangles=%d",
            cad_body_count,
            volume_mm3 / 1000.0,
            len(metrics_mesh.faces),
            extra={
                "event": "metrics_computed",
                "body_count": cad_body_count,
                "volume_cm3": volume_mm3 / 1000.0,
                "triangle_count": len(metrics_mesh.faces),
                "is_manifold": is_manifold,
            },
        )

        return {
            "volume_cm3": volume_mm3 / 1000.0,
            "support_volume_cm3": support_mm3 / 1000.0,
            "surface_area_cm2": area_mm2 / 100.0,
            "bounding_box": bbox,
            "is_manifold": is_manifold,
            "non_manifold_reason": non_manifold_reason,
            "non_manifold_face_count": non_manifold_face_count,
            "triangle_count": len(metrics_mesh.faces),
            "euler_number": euler_number,
            "body_count": cad_body_count,
            "body_names": body_names,
            "body_volumes_cm3": [
                abs(float(m.volume)) / 1000.0 if m.is_watertight else None
                for m in mesh_list
            ],
            "mesh_stl_bytes_dict": mesh_stl_bytes_dict,  # Per-body STL bytes
            "mesh_stl_bytes": mesh_stl_bytes,  # Legacy: first body only
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

    # STEP/IGES files loaded via trimesh often return meter-scale values
    # (matching the glTF convention).  If all dimensions are < 1 unit, the
    # mesh is almost certainly in meters — scale to mm so metrics are correct.
    if ext in ("step", "stp", "igs", "iges", "glb", "gltf"):  # noqa: SIM102
        if mesh.extents is not None and max(mesh.extents.tolist()) < 1.0:
            mesh.apply_scale(1000.0)
            logger.info(
                f"FALLBACK: Detected meter-scale {ext}, applied ×1000 for mm",
                extra={"event": "trimesh_fallback_scale", "extension": ext},
            )

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

    # Export GLB for unified Phase 2 pipeline
    fallback_glb_bytes: bytes | None = None
    try:
        fallback_glb_bytes = cast(bytes, mesh.export(file_type="glb"))
    except Exception as ex:
        logger.warning(
            f"GLB export failed for {ext} mesh: {ex}",
            extra={"event": "fallback_glb_export_failed", "extension": ext},
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
        "cad_glb_bytes": fallback_glb_bytes,
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
                    f"Loading CAD file with gmsh (timeout={GMSH_MESH_TIMEOUT_SECONDS}s)",  # noqa: E501
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
                    if (
                        not isinstance(dumped_mesh, trimesh.Trimesh)
                        or len(dumped_mesh.vertices) == 0
                    ):
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
                    "Mesh is not manifold (not watertight) — previews generated with best-effort geometry"  # noqa: E501
                )
        else:
            logger.warning(
                "Mesh has no polygon faces — skipping preview generation (point cloud or line mesh)"  # noqa: E501
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
    """Phase 2 worker: renders a 256px isometric thumbnail from STL bytes. Runs in a separate process."""  # noqa: E501
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


def _render_thumbnail_from_glb_worker(glb_bytes: bytes) -> bytes | None:
    """Phase 2 worker: renders a 256px isometric thumbnail from GLB bytes directly.
    Uses headless matplotlib rendering (Kubernetes-friendly).
    Falls back to PyVista if matplotlib unavailable.
    """
    try:
        from src.core.headless_thumbnail import render_thumbnail_from_glb_headless

        # Try headless matplotlib rendering first (truly headless, K8s-friendly)
        thumbnail = render_thumbnail_from_glb_headless(
            glb_bytes, size=256, format="png"
        )
        if thumbnail:
            logger.info(
                "Successfully rendered thumbnail using headless matplotlib renderer"
            )
            return thumbnail
        logger.warning("Headless rendering failed, falling back to PyVista")

    except ImportError:
        logger.info("Headless thumbnail renderer not available, using PyVista fallback")
    except Exception as e:
        logger.warning(f"Headless rendering failed: {e}, falling back to PyVista")

    # Fallback to PyVista-based rendering
    return _render_thumbnail_from_glb_worker_fallback(glb_bytes)


def _render_thumbnail_from_glb_worker_fallback(glb_bytes: bytes) -> bytes | None:
    """Fallback thumbnail rendering using PyVista (legacy method)."""
    try:
        import numpy as np
        import pyvista as pv
        import trimesh

        pv.OFF_SCREEN = True

        # Load GLB using trimesh (supports glTF/GLB, including multi-body Scenes)
        tmesh = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")

        # trimesh.load returns a Scene for multi-body GLBs — dump to a single Trimesh
        if isinstance(tmesh, trimesh.Scene):
            tmesh = tmesh.dump(concatenate=True)
        if not isinstance(tmesh, trimesh.Trimesh) or len(tmesh.vertices) == 0:
            logger.warning(
                "_render_thumbnail_from_glb_worker_fallback: empty or non-Trimesh after load"  # noqa: E501
            )
            return None

        vertices = tmesh.vertices
        faces = tmesh.faces  # shape (N, 3) — trimesh triangles, no count prefix

        # PyVista PolyData requires faces as flat array: [3, v0, v1, v2, 3, v0, v1, v2, ...]  # noqa: E501
        faces_pv = np.hstack(
            [np.full((len(faces), 1), 3, dtype=np.int32), faces]
        ).flatten()

        # Create PyVista mesh from vertices and faces
        mesh = pv.PolyData(vertices.copy(), faces_pv)
        if not mesh or mesh.n_points == 0:
            return None

        # Center the mesh
        mesh_center = mesh.center
        mesh.points -= mesh_center

        # Extract feature edges for visual clarity
        feat_edges = mesh.extract_feature_edges(
            boundary_edges=False,
            feature_edges=True,
            manifold_edges=False,
            non_manifold_edges=False,
            feature_angle=30,
        )

        # Compute normals for proper lighting
        mesh.compute_normals(
            cell_normals=True, point_normals=True, split_vertices=True, inplace=True
        )

        tb = np.array(mesh.bounds)
        max_dim = float(np.max([tb[1] - tb[0], tb[3] - tb[2], tb[5] - tb[4]]))
        iso_cfg = VIEW_CONFIGS["iso"]
        return _render_single_view(
            pv_mesh=mesh,
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
        logger.warning(f"_render_thumbnail_from_glb_worker_fallback failed: {e}")
        return None


def _export_glb_worker(cad_glb_bytes: bytes, file_ext: str = "") -> bytes | None:
    """Phase 2 worker: returns GLB bytes for the 3D viewer in Y-up, millimeters.

    GLB-first pipeline: all formats produce GLB in Phase 1, Phase 2 always uses GLB.

    Unit handling by source format:
    - STEP/IGES: cascadio outputs GLB in meters (glTF spec) → apply ×1000 to get mm
    - STL/OBJ/3MF: trimesh loads in mm → Phase 1 exports GLB already in mm → no scale
    - GLB upload: Phase 1 applies heuristic ×1000 if meter-scale → mm → no scale

    Coordinate convention: all GLBs served to the viewer are **Y-up, mm**.
    The viewer (part-viewer.js) applies a +90° X rotation at load time to
    display the model in Z-up (CAD standard: X=right, Y=depth, Z=up).

    Args:
        cad_glb_bytes: GLB bytes from Phase 1 (may be meters or mm depending on source)
        file_ext: Original file extension (with dot, e.g., ".step", ".stl") to determine units
    """  # noqa: E501
    # Z-up → Y-up pre-rotation.
    # −90° around X: (x, y, z) → (x, z, −y) — original Z (up) becomes glTF Y (up).
    z_to_yup = np.array(
        [[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=float
    )

    try:
        # Load GLB (from Phase 1 - cascadio for STEP, trimesh export for STL/OBJ/3MF)
        scene_data = trimesh.load(io.BytesIO(cad_glb_bytes), file_type="glb")

        # STEP/IGES: cascadio outputs GLB in meters (glTF spec) → scale to mm
        # STL/OBJ/3MF: trimesh exports GLB already in mm → no scale
        # GLB upload: Phase 1 already scaled to mm → no scale
        if file_ext in (".step", ".stp", ".igs", ".iges"):
            scene_data.apply_scale(1000.0)

        if isinstance(scene_data, trimesh.Scene) and len(scene_data.geometry) > 1:
            # Multi-body: preserve node hierarchy so the viewer can color/select bodies.
            try:  # noqa: SIM105
                scene_data.apply_translation(-scene_data.centroid)  # center at origin
            except Exception:
                pass
            scene_data.apply_transform(z_to_yup)  # Z-up (OCCT) → Y-up (glTF)
            return cast(bytes, scene_data.export(file_type="glb"))

        # Single-body fallback: flatten to Trimesh (backward compatible).
        if isinstance(scene_data, trimesh.Scene):
            mesh = scene_data.dump(concatenate=True)
        else:
            mesh = scene_data

        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.vertices) == 0:
            return None

        mesh.apply_translation(-mesh.center_mass)  # center at origin
        mesh.apply_transform(z_to_yup)  # Z-up (OCCT) → Y-up (glTF)
        return cast(bytes, mesh.export(file_type="glb"))

    except Exception as e:
        logger.warning(f"_export_glb_worker failed: {e}", exc_info=True)
        return None


def _quick_quality_check(
    stl_bytes: bytes,
    cad_bytes: bytes | None = None,
    cad_extension: str | None = None,
) -> dict[str, Any]:
    """Perform quick quality checks on a single body.

    Returns quality metrics in <5 seconds for any file size.
    This is Phase 1 of the two-phase DFM analysis architecture.

    Quality checks include:
    - Manifold/watertight verification
    - Multi-body detection
    - Basic geometry metrics (volume, bounding box, surface area)
    - Face/vertex counts for complexity assessment

    Args:
        stl_bytes: STL file data as bytes
        cad_bytes: Optional CAD file data (STEP/IGES) for B-Rep metadata
        cad_extension: CAD file extension (e.g., "step", "stp")

    Returns:
        Quality check results dict with keys:
        - is_manifold: bool - whether mesh is watertight
        - is_empty: bool - whether mesh has no faces
        - face_count: int - number of triangular faces
        - vertex_count: int - number of vertices
        - volume_mm3: float - volume in cubic millimeters
        - surface_area_mm2: float - surface area in square millimeters
        - bounding_box: dict with x, y, z dimensions in mm
        - can_preview: bool - whether file can be displayed
        - complexity: str - "simple", "medium", "complex" based on face count
        - body_count: int - always 1 for single-body files
    """
    import time

    start_time = time.time()

    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            return {
                "is_manifold": False,
                "is_empty": True,
                "error": "Invalid mesh format",
            }

        # Basic mesh properties
        is_manifold, non_manifold_reason, non_manifold_face_count = _mesh_manifold_info(
            mesh
        )
        is_empty = len(mesh.faces) == 0
        face_count = len(mesh.faces)
        vertex_count = len(mesh.vertices)
        body_count = len(_split_significant_mesh_bodies(mesh))

        if is_empty:
            elapsed = time.time() - start_time
            logger.info(
                f"Quality check completed in {elapsed:.2f}s "
                f"(faces={face_count}, complexity=simple)"
            )
            return {
                "is_manifold": False,
                "is_empty": True,
                "face_count": 0,
                "vertex_count": 0,
                "complexity": "simple",
                "error": "empty_mesh",
                "error_detail": "STL parsing produced 0 faces — bytes may not be valid STL.",  # noqa: E501
            }

        # Geometry metrics
        extents = (
            mesh.extents if mesh.extents is not None else np.array([0.0, 0.0, 0.0])
        )
        volume_mm3 = float(mesh.volume) if is_manifold else 0.0
        surface_area_mm2 = float(mesh.area)

        # Bounding box
        bounding_box = {
            "x": float(extents[0]),
            "y": float(extents[1]),
            "z": float(extents[2]),
        }

        # Complexity classification
        if face_count < 1000:
            complexity = "simple"
        elif face_count < 10000:
            complexity = "medium"
        else:
            complexity = "complex"

        # Estimate B-Rep face count from CAD if available.
        # T1c: Use topology-only walk (no tessellation) — avoids the expensive
        # BRepMesh_IncrementalMesh call that analyze_step_brep would trigger.
        brep_face_count = None
        if cad_bytes and cad_extension in ("step", "stp", "igs", "iges"):
            try:
                import os as _os
                import tempfile as _tmpmod

                import cadquery as cq
                from OCP.TopAbs import TopAbs_FACE as _TopAbs_FACE
                from OCP.TopExp import TopExp_Explorer as _TopExp_Explorer

                with _tmpmod.NamedTemporaryFile(
                    suffix=f".{cad_extension}", delete=False
                ) as _tmp:
                    _tmp.write(cad_bytes)
                    _tmp_path = _tmp.name
                try:
                    _shape = (
                        cq.importers.importStep(_tmp_path)
                        if cad_extension in ("step", "stp")
                        else cq.importers.importShape(_tmp_path)
                    )
                    if _shape is not None:
                        _exp = _TopExp_Explorer(_shape.val().wrapped, _TopAbs_FACE)
                        _cnt = 0
                        while _exp.More():
                            _cnt += 1
                            _exp.Next()
                        brep_face_count = _cnt
                finally:
                    with contextlib.suppress(OSError):
                        _os.unlink(_tmp_path)  # noqa: PTH108
            except Exception as occ_err:
                logger.warning("OCC topology face count failed: %s", occ_err)

        elapsed = time.time() - start_time
        logger.info(
            f"Quality check completed in {elapsed:.2f}s "
            f"(faces={face_count}, complexity={complexity})"
        )

        return {
            "is_manifold": is_manifold,
            "is_empty": is_empty,
            "face_count": face_count,
            "vertex_count": vertex_count,
            "volume_mm3": volume_mm3,
            "surface_area_mm2": surface_area_mm2,
            "bounding_box": bounding_box,
            "can_preview": not is_empty,
            "complexity": complexity,
            "body_count": body_count,
            "non_manifold_reason": non_manifold_reason,
            "non_manifold_face_count": non_manifold_face_count,
            "brep_face_count": brep_face_count,
        }

    except Exception as e:
        logger.warning("Quick quality check failed: %s", e)
        return {
            "is_manifold": False,
            "is_empty": True,
            "error": str(e),
        }


def _analyze_single_process(
    stl_bytes: bytes,
    process_code: str,
    cad_bytes: bytes | None = None,
    cad_extension: str | None = None,
    shared_precomputed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze a single body for a SPECIFIC manufacturing process.

    This is Phase 2 of the two-phase DFM analysis architecture.
    Only analyzes the requested process, not all 8+ processes.
    Returns in <15 seconds for typical files.

    Args:
        stl_bytes: STL file data as bytes
        process_code: Manufacturing process code (e.g., "FDM", "SLA", "CNC_MILL")
        cad_bytes: Optional CAD file data (STEP/IGES) for B-Rep analysis
        cad_extension: CAD file extension (e.g., "step", "stp")
        shared_precomputed: Optional dict of pre-computed data from quality check
                          to avoid redundant computation

    Returns:
        DFM report dict for the requested process only.
        Returns error dict with "error_type" key if analysis fails.
    """
    import time

    start_time = time.time()

    try:
        from src.core.cnc_analyzers import (
            compute_sharp_corner_analysis,  # noqa: F401
            detect_axial_symmetry,  # noqa: F401
            detect_cavities,  # noqa: F401
            detect_chatter_risk,  # noqa: F401
            detect_deep_narrow_cavities,  # noqa: F401
            detect_grooves,  # noqa: F401
            detect_internal_radii,  # noqa: F401
            detect_tool_access,  # noqa: F401
        )
        from src.core.dfm_thresholds import (
            MILLING_RULES,  # noqa: F401
            PRINTING_RULES,
            TURNING_RULES,  # noqa: F401
            get_tool_for_radius,  # noqa: F401
        )
        from src.core.mesh_analyzers import (
            compute_overhang_analysis,  # noqa: F401
            compute_thin_wall_analysis,  # noqa: F401
            compute_unsupported_wall_analysis,  # noqa: F401
            detect_bridges,  # noqa: F401
            detect_connecting_clearance,  # noqa: F401
            detect_embossed_engraved,  # noqa: F401
            detect_escape_hole_risk,  # noqa: F401
            detect_holes_mesh,
            detect_hollow_regions,
            detect_small_features,  # noqa: F401
            detect_small_features_occ,  # noqa: F401
            detect_thin_pins,  # noqa: F401
        )
        from src.core.occ_analyzer import analyze_step_brep
    except ImportError as _imp_err:
        logger.warning("DFM analysis modules unavailable: %s", _imp_err)
        return {"error_type": "ImportError", "message": str(_imp_err)}

    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            return {"error_type": "ValueError", "message": "Invalid mesh format"}
        if len(mesh.faces) == 0:
            return {
                "error_type": "EmptyMesh",
                "message": "STL bytes produced an empty mesh (0 faces). Input may not be valid STL.",  # noqa: E501
            }

        # Sanitize mesh to remove degenerate/unreferenced vertices that cause
        # trimesh.geometry.vertex_face_indices count-mismatch crashes.
        mesh = mesh.copy()
        mesh.process(validate=True)
        mesh.remove_unreferenced_vertices()
        mesh.merge_vertices()
        mesh.update_faces(mesh.unique_faces())
        mesh.update_faces(mesh.nondegenerate_faces())

        extents = (
            mesh.extents if mesh.extents is not None else np.array([0.0, 0.0, 0.0])
        )
        vol_bbox = float(extents[0]) * float(extents[1]) * float(extents[2])
        volume_mm3 = float(mesh.volume) if mesh.is_watertight else 0.0
        support_mm3 = max(0.0, vol_bbox - volume_mm3)

        # ── OCC B-Rep analysis (STEP/IGES only) ─────────────────────────────
        # T1b: Cache tessellation results — avoids re-running BRepMesh for each
        # FDM/SLA/CNC click on the same file.  Cache lives in the process
        # address space so it works for both:
        #   • dfm_executor worker (max_tasks_per_child=5 → spans a user session)
        #   • main process thread pool (API path, shared across all threads)
        import time as _time

        occ_features: list[Any] = []
        occ_face_tag_to_tri: dict[int, list[int]] = {}
        if cad_bytes and cad_extension in ("step", "stp", "igs", "iges"):
            try:
                from src.core.geometry_optimizations import (
                    get_tessellation_tolerance as _get_tol,
                )

                _file_size_mb = len(cad_bytes) / (1024 * 1024)
                _tol = _get_tol(process_code, _file_size_mb)
                _occ_key = _occ_cache_key(cad_bytes, _tol)
                _cached = _occ_cache.get(_occ_key)
                if _cached is not None:
                    _cf, _ct, _cat = _cached
                    if _time.time() - _cat < _OCC_CACHE_TTL:
                        occ_features, occ_face_tag_to_tri = _cf, _ct
                        logger.info(
                            "OCC cache HIT process=%s tol=%.2f", process_code, _tol
                        )
                    else:
                        del _occ_cache[_occ_key]

                if not occ_features:
                    occ_features, occ_face_tag_to_tri = analyze_step_brep(
                        cad_bytes, cad_extension, process_code
                    )
                    if len(_occ_cache) >= _OCC_CACHE_MAX:
                        del _occ_cache[next(iter(_occ_cache))]
                    _occ_cache[_occ_key] = (
                        occ_features,
                        occ_face_tag_to_tri,
                        _time.time(),
                    )
                    logger.info(
                        "OCC cache MISS — tessellated+cached process=%s tol=%.2f features=%d",  # noqa: E501
                        process_code,
                        _tol,
                        len(occ_features),
                    )
            except Exception as _occ_err:
                logger.warning(
                    "OCC analysis failed — using mesh-only path: %s", _occ_err
                )

        # ── Pre-compute shared data (if not provided) ────────────────────────
        # T1d: mesh_precompute_cache avoids re-running detect_hollow_regions and
        # detect_holes_mesh for each FDM/SLA/CNC click on the same body.
        if shared_precomputed:
            hollow_centroids = shared_precomputed.get("hollow_centroids", [])
            all_holes = shared_precomputed.get("all_holes", [])
        else:
            _mkey = _mesh_cache_key(stl_bytes)
            _mcached = _mesh_precompute_cache.get(_mkey)
            if (
                _mcached is not None
                and _time.time() - _mcached["cached_at"] < _OCC_CACHE_TTL
            ):
                hollow_centroids = _mcached["hollow_centroids"]
                all_holes = _mcached["all_holes"]
                logger.debug("Mesh precompute cache HIT key=%s", _mkey)
            else:
                hollow_centroids, _ = detect_hollow_regions(mesh)
                # T2e: compute at 0.1 mm (smallest threshold) so this list
                # covers both the 1.0 mm hole filter AND thin-pin detection.
                all_holes = detect_holes_mesh(mesh, min_diameter_mm=0.1)
                if len(_mesh_precompute_cache) >= _MESH_PRECOMPUTE_CACHE_MAX:
                    del _mesh_precompute_cache[next(iter(_mesh_precompute_cache))]
                _mesh_precompute_cache[_mkey] = {
                    "hollow_centroids": hollow_centroids,
                    "all_holes": all_holes,
                    "cached_at": _time.time(),
                }
                logger.debug(
                    "Mesh precompute cache MISS — computed+cached key=%s", _mkey
                )

        issues: list[dict[str, Any]] = []

        # ── Process-specific analysis ───────────────────────────────────────
        if process_code in PRINTING_RULES:
            rules = PRINTING_RULES[process_code]
            issues.extend(
                _analyze_printing_process(
                    mesh, rules, process_code, occ_features, all_holes, support_mm3
                )
            )

        elif process_code in ("CNC_MILL", "CNC"):
            issues.extend(_analyze_cnc_milling(mesh, occ_features, all_holes))

        elif process_code == "CNC_TURN":
            issues.extend(_analyze_cnc_turning(mesh, occ_features))

        else:
            return {
                "error_type": "ValueError",
                "message": f"Unknown process code: {process_code}",
            }

        elapsed = time.time() - start_time
        logger.info(f"Process {process_code} analysis completed in {elapsed:.2f}s")

        # Generate report
        report = {
            "reportType": process_code,
            "issues": issues,
            "analysis_time_seconds": elapsed,
        }

        # Add process-specific summary fields
        if process_code in PRINTING_RULES:
            report.update(_generate_printing_summary(issues))
        elif process_code in ("CNC_MILL", "CNC"):
            report.update(_generate_cnc_milling_summary(issues, mesh))
        elif process_code == "CNC_TURN":
            report.update(_generate_cnc_turning_summary(issues))

        return report

    except Exception as e:
        logger.warning("_analyze_single_process failed for %s: %s", process_code, e)
        return {"error_type": type(e).__name__, "message": str(e)}
    finally:
        # Release heavy objects regardless of success/failure path.
        # trimesh/numpy hold large C-extension buffers that Python's cyclic GC
        # won't reclaim until a collection cycle runs — calling gc.collect()
        # here ensures RSS drops between per-body tasks even when summary
        # generation raises.
        import gc

        with contextlib.suppress(NameError):
            del mesh
        with contextlib.suppress(NameError):
            del issues
        with contextlib.suppress(NameError):
            del occ_features
        with contextlib.suppress(NameError):
            del occ_face_tag_to_tri
        gc.collect()


def _analyze_single_body(
    stl_bytes: bytes,
    cad_bytes: bytes | None = None,
    cad_extension: str | None = None,
) -> dict[str, Any]:
    """Run DFM analysis on a single body. Returns report dict.

    Runs FDM analysis immediately (fits within per-body timeout budget).
    SLA and CNC receive valid empty reports so the frontend resolves out of
    "Analyzing..." state regardless of the user's selected process.
    """
    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            return {}

        # Sanitize mesh to remove degenerate/unreferenced vertices that cause
        # trimesh.geometry.vertex_face_indices count-mismatch crashes.
        mesh = mesh.copy()
        mesh.process(validate=True)
        mesh.remove_unreferenced_vertices()
        mesh.merge_vertices()
        mesh.update_faces(mesh.unique_faces())
        mesh.update_faces(mesh.nondegenerate_faces())

        # Quality metrics (lightweight, always computed)
        reports: dict[str, Any] = {
            "quality": {
                "is_manifold": mesh.is_watertight,
                "is_empty": len(mesh.faces) == 0,
                "face_count": len(mesh.faces),
                "body_count": 1,
                "volume_mm3": float(mesh.volume) if mesh.is_watertight else 0.0,
                "bounding_box": {
                    "x": float(mesh.extents[0]) if mesh.extents is not None else 0.0,
                    "y": float(mesh.extents[1]) if mesh.extents is not None else 0.0,
                    "z": float(mesh.extents[2]) if mesh.extents is not None else 0.0,
                },
                "surface_area_mm2": float(mesh.area) if hasattr(mesh, "area") else 0.0,
                "complexity": "simple"
                if len(mesh.faces) < 1000
                else "complex"
                if len(mesh.faces) > 10000
                else "medium",
            },
            "hollow_regions": [],
        }

        # ── FDM analysis (real results, <15 s per body) ───────────────────────
        fdm_result = _analyze_single_process(stl_bytes, "FDM", cad_bytes, cad_extension)
        if fdm_result and "error_type" not in fdm_result:
            reports["FDM"] = fdm_result
        else:
            # Fall back to a valid empty FDM report so the consumer doesn't publish null
            reports["FDM"] = {
                "reportType": "FDM",
                "thinWallCount": 0,
                "thinWallRegions": [],
                "overhangFaceCount": 0,
                "overhangAreaCm2": 0.0,
                "overhangRegions": [],
                "supportRequired": False,
                "estimatedSupportVolumeCm3": None,
                "smallDetailCount": 0,
                "issues": [],
            }

        # ── Valid empty SLA/CNC reports (no twoPhaseDeferred) ─────────────────
        # These are published as non-null so the frontend resolves DfmReport for
        # any process code, clearing the "Analyzing..." state immediately.
        reports["SLA"] = {
            "reportType": "SLA",
            "thinWallCount": 0,
            "thinWallRegions": [],
            "overhangFaceCount": 0,
            "overhangAreaCm2": 0.0,
            "overhangRegions": [],
            "supportRequired": False,
            "estimatedSupportVolumeCm3": None,
            "smallDetailCount": 0,
            "resinTrappingRisk": False,
            "resinTrappingRegions": [],
            "suctionRisk": False,
            "suctionRegions": [],
            "hollowRegions": [],
            "issues": [],
        }
        reports["CNC"] = {
            "reportType": "CNC",
            "sharpCornerCount": 0,
            "sharpCornerRegions": [],
            "hasUndercuts": False,
            "undercutRegions": [],
            "hasDrillHoles": False,
            "issues": [],
        }

        logger.info(
            "_analyze_single_body completed FDM analysis. "
            "Faces: %d, Manifold: %s, FDM issues: %d",
            len(mesh.faces),
            mesh.is_watertight,
            len(reports["FDM"].get("issues", [])),
        )

        return reports

    except Exception as e:
        logger.warning("_analyze_single_body failed: %s", e)
        return {}


def _analyze_printing_process(
    mesh: trimesh.Trimesh,
    rules: Any,
    process_code: str,
    occ_features: list[Any],
    all_holes: list[Any],
    support_mm3: float,  # noqa: ARG001
    bodies: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Analyze mesh for a specific 3D printing process.

    Args:
        mesh: Trimesh object
        rules: Process-specific DFM rules from PRINTING_RULES
        process_code: Process code (e.g., "FDM", "SLA")
        occ_features: Optional OCC B-Rep features for precision analysis
        all_holes: Pre-computed hole detections at 0.1 mm threshold.
        support_mm3: Pre-computed support volume estimate
        bodies: Optional pre-split body list to avoid repeated mesh.split() calls.

    Returns:
        List of DFM issue dicts
    """
    from src.core.mesh_analyzers import (
        compute_overhang_analysis,
        compute_thin_wall_analysis,
        compute_unsupported_wall_analysis,
        detect_bridges,
        detect_connecting_clearance,
        detect_embossed_engraved,
        detect_escape_hole_risk,
        detect_small_features,
        detect_small_features_occ,
        detect_thin_pins,
    )

    # T2c/T2d: split mesh once here so detect_escape_hole_risk and
    # detect_connecting_clearance don't each call mesh.split() independently.
    if bodies is None and (
        rules.escape_hole_diameter_mm is not None
        or rules.connecting_clearance_mm is not None
    ):
        try:
            import trimesh as _trimesh

            _split = mesh.split()
            if isinstance(_split, _trimesh.Scene):
                bodies = list(_split.geometry.values())
            elif isinstance(_split, _trimesh.Trimesh):
                bodies = [_split]
        except Exception as _split_err:
            logger.debug("mesh.split() failed: %s", _split_err)
            bodies = None

    issues: list[dict[str, Any]] = []

    # Thin wall (supported)
    try:
        tw_count, tw_centroids, tw_face_idx = compute_thin_wall_analysis(
            mesh, rules.supported_wall_mm
        )
    except Exception as _e:
        logger.warning("Thin wall detection failed: %s", _e)
        tw_count, tw_centroids, tw_face_idx = 0, [], []
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
                "centroid": tw_centroids[0] if tw_centroids else [0.0, 0.0, 0.0],
                "metadata": {},
            }
        )

    # Unsupported wall (not applicable for powder-bed processes)
    uw_count, uw_centroids, uw_face_idx = 0, [], []
    if rules.unsupported_wall_mm is not None:
        try:
            uw_count, uw_centroids, uw_face_idx = compute_unsupported_wall_analysis(
                mesh, rules.unsupported_wall_mm
            )
        except Exception as _e:
            logger.warning("Unsupported wall detection failed: %s", _e)
            uw_count, uw_centroids, uw_face_idx = 0, [], []
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
                    "centroid": uw_centroids[0] if uw_centroids else [0.0, 0.0, 0.0],
                    "metadata": {},
                }
            )

    # Overhang (not applicable for powder-bed processes)
    # OPTIMIZATION: Skip overhang check for powder-bed processes (SLS, MJF, BJ, DMLS)
    powder_bed_processes = ["SLS", "MJF", "BJ", "DMLS"]
    oh_count, oh_area_cm2, oh_centroids, oh_face_idx = 0, 0.0, [], []
    if rules.max_overhang_deg is not None and process_code not in powder_bed_processes:
        try:
            (
                oh_count,
                oh_area_cm2,
                oh_centroids,
                oh_face_idx,
            ) = compute_overhang_analysis(
                mesh,
                rules.max_overhang_deg,
            )
        except Exception as _e:
            logger.warning("Overhang detection failed: %s", _e)
            oh_count, oh_area_cm2, oh_centroids, oh_face_idx = 0, 0.0, [], []
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
                    "centroid": oh_centroids[0] if oh_centroids else [0.0, 0.0, 0.0],
                    "metadata": {
                        "areaCm2": float(oh_area_cm2),
                        "regionCount": oh_count,
                    },
                }
            )

    # Holes below process minimum diameter
    small_holes = [h for h in all_holes if h.diameter_mm < rules.min_hole_diameter_mm]
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
    # OPTIMIZATION: Skip bridge check for powder-bed processes (SLS, MJF, BJ, DMLS)
    if rules.bridge_span_mm is not None and process_code not in powder_bed_processes:
        try:
            br_count, br_centroids, br_face_idx = detect_bridges(
                mesh,
                rules.bridge_span_mm,
            )
        except Exception as _e:
            logger.warning("Bridge detection failed: %s", _e)
            br_count, br_centroids, br_face_idx = 0, [], []
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
                    "centroid": br_centroids[0] if br_centroids else [0.0, 0.0, 0.0],
                    "metadata": {},
                }
            )

    # Small features — use CAD topology when available (STEP/IGES)
    try:
        if occ_features:
            sf_count, sf_centroids, sf_face_idx = detect_small_features_occ(
                occ_features, rules.min_feature_mm, mesh
            )
        else:
            sf_count, sf_centroids, sf_face_idx = detect_small_features(
                mesh, rules.min_feature_mm
            )
    except Exception as _e:
        logger.warning("Small features detection failed: %s", _e)
        sf_count, sf_centroids, sf_face_idx = 0, [], []
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
                "centroid": sf_centroids[0] if sf_centroids else [0.0, 0.0, 0.0],
                "metadata": {},
            }
        )

    # Thin pins / columns — pass all_holes so detect_thin_pins skips its own
    # detect_holes_mesh call (T2e: holes already computed at 0.1 mm threshold).
    try:
        pin_count, pin_centroids, pin_face_idx = detect_thin_pins(
            mesh, rules.pin_diameter_mm, precomputed_holes=all_holes
        )
    except Exception as _e:
        logger.warning("Thin pin detection failed: %s", _e)
        pin_count, pin_centroids, pin_face_idx = 0, [], []
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
                "centroid": pin_centroids[0] if pin_centroids else [0.0, 0.0, 0.0],
                "metadata": {},
            }
        )

    # Escape holes (enclosed volumes without drainage)
    if rules.escape_hole_diameter_mm is not None:
        try:
            esc_has_risk, esc_centroids, esc_face_idx = detect_escape_hole_risk(
                mesh,
                rules.escape_hole_diameter_mm,
                bodies=bodies,
                precomputed_holes=all_holes,
            )
        except Exception as _e:
            logger.warning("Escape hole detection failed: %s", _e)
            esc_has_risk, esc_centroids, esc_face_idx = False, [], []
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
                    "centroid": esc_centroids[0] if esc_centroids else [0.0, 0.0, 0.0],
                    "metadata": {},
                }
            )

    # Connecting clearance (multi-body assemblies)
    if rules.connecting_clearance_mm is not None:
        try:
            cl_count, cl_centroids, cl_face_idx = detect_connecting_clearance(
                mesh,
                rules.connecting_clearance_mm,
                bodies=bodies,
            )
        except Exception as _e:
            logger.warning("Connecting clearance detection failed: %s", _e)
            cl_count, cl_centroids, cl_face_idx = 0, [], []
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
                    "centroid": cl_centroids[0] if cl_centroids else [0.0, 0.0, 0.0],
                    "metadata": {},
                }
            )

    # Embossed / engraved features
    try:
        emb_count, emb_centroids, emb_face_idx = detect_embossed_engraved(
            mesh, rules.embossed_width_mm, rules.embossed_height_mm
        )
    except Exception as _e:
        logger.warning("Embossed/engraved detection failed: %s", _e)
        emb_count, emb_centroids, emb_face_idx = 0, [], []
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
                "centroid": emb_centroids[0] if emb_centroids else [0.0, 0.0, 0.0],
                "metadata": {
                    "minWidthMm": rules.embossed_width_mm,
                    "minHeightMm": rules.embossed_height_mm,
                },
            }
        )

    return issues


def _analyze_cnc_milling(
    mesh: trimesh.Trimesh,
    occ_features: list[Any],  # noqa: ARG001
    all_holes: list[Any],
) -> list[dict[str, Any]]:
    """Analyze mesh for CNC milling process.

    Args:
        mesh: Trimesh object
        occ_features: Optional OCC B-Rep features for precision analysis
        all_holes: Pre-computed hole detections

    Returns:
        List of DFM issue dicts
    """
    from src.core.cnc_analyzers import (
        compute_sharp_corner_analysis,
        detect_cavities,
        detect_chatter_risk,
        detect_deep_narrow_cavities,
        detect_internal_radii,
        detect_tool_access,
    )
    from src.core.dfm_thresholds import MILLING_RULES, get_tool_for_radius

    issues: list[dict[str, Any]] = []

    # Internal radii
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
        issues.append(
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

    # Deep cavities
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
        issues.append(
            {
                "category": "cavity_depth",
                "severity": "error" if max_dr > 8.0 else "warning",
                "title": f"Deep Cavities ({dc_count})",
                "description": (
                    f"{dc_count} cavity/cavities exceed the {MILLING_RULES.cavity_depth_ratio}:1"  # noqa: E501
                    f" depth/width limit. Worst: {max_dr:.1f}:1."
                ),
                "value": float(max_dr),
                "threshold": float(MILLING_RULES.cavity_depth_ratio),
                "faceIndices": dc_face_idx[:2000],
                "centroid": dc_centroids[0] if dc_centroids else [0.0, 0.0, 0.0],
                "metadata": {"maxDepthRatio": float(max_dr)},
            }
        )

    # Tool access
    tool_access = detect_tool_access(mesh)
    if tool_access.minimum_axes > 3:
        issues.append(
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

    # Chatter risk
    ch_count, ch_centroids, ch_face_idx = detect_chatter_risk(mesh)
    if ch_count > 0:
        issues.append(
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

    # Sharp corners
    sc_count, sc_centroids, sc_face_idx = compute_sharp_corner_analysis(mesh, 45.0)
    if sc_count > 0:
        issues.append(
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

    # Deep drill holes
    cnc_holes = all_holes if all_holes else []
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
        issues.append(
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

    return issues


def _analyze_cnc_turning(
    mesh: trimesh.Trimesh,
    occ_features: list[Any],  # noqa: ARG001
) -> list[dict[str, Any]]:
    """Analyze mesh for CNC turning process.

    Args:
        mesh: Trimesh object
        occ_features: Optional OCC B-Rep features for precision analysis

    Returns:
        List of DFM issue dicts
    """
    from src.core.cnc_analyzers import (
        _compute_z_slice_profile,
        detect_axial_symmetry,
        detect_grooves,
    )
    from src.core.dfm_thresholds import TURNING_RULES

    # T3e: compute Z-slice profile once; share between detect_axial_symmetry
    # and detect_grooves so only one section_multiplane pass is needed.
    _slice_profile = _compute_z_slice_profile(mesh, n_slices=100)

    issues: list[dict[str, Any]] = []
    axis_report = detect_axial_symmetry(mesh, slice_profile=_slice_profile)

    if not axis_report.is_turnable:
        issues.append(
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
        # Length/diameter ratio
        ld_ratio = axis_report.length_diameter_ratio or 0.0
        if ld_ratio > TURNING_RULES.max_length_diameter_ratio:
            issues.append(
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

        # Grooves — reuse the already-computed slice profile (T3e)
        grooves = detect_grooves(mesh, slice_profile=_slice_profile)
        narrow_grooves = [
            g for g in grooves if g.width_mm < TURNING_RULES.min_groove_width_mm
        ]
        if narrow_grooves:
            ng_face_idx: list[int] = []
            for g in narrow_grooves:
                ng_face_idx.extend(g.face_indices)
            min_gw = min(g.width_mm for g in narrow_grooves)
            issues.append(
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

    return issues


def _generate_printing_summary(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate legacy summary fields for printing processes.

    Extracts key metrics from issues list for backward compatibility.
    """
    summary = {
        "thinWallCount": 0,
        "thinWallRegions": [],
        "overhangFaceCount": 0,
        "overhangAreaCm2": 0.0,
        "overhangRegions": [],
        "supportRequired": False,
        "estimatedSupportVolumeCm3": None,
        "smallDetailCount": 0,
    }

    for issue in issues:
        cat = issue.get("category", "")

        if cat == "thin_wall":
            summary["thinWallCount"] = issue.get("value", 0)
            summary["thinWallRegions"] = [issue.get("centroid", [0, 0, 0])]
            summary["supportRequired"] = True

        elif cat == "overhang":
            summary["overhangFaceCount"] = issue.get("metadata", {}).get(
                "regionCount", 0
            )
            summary["overhangAreaCm2"] = issue.get("value", 0.0)
            summary["overhangRegions"] = [issue.get("centroid", [0, 0, 0])]
            summary["supportRequired"] = True

        elif cat == "unsupported_wall":
            summary["supportRequired"] = True

        elif cat in ("small_feature", "pin"):
            summary["smallDetailCount"] += issue.get("value", 0)

    return summary


def _generate_cnc_milling_summary(
    issues: list[dict[str, Any]],
    mesh: trimesh.Trimesh,  # noqa: ARG001
) -> dict[str, Any]:
    """Generate legacy summary fields for CNC milling process.

    Extracts key metrics from issues list for backward compatibility.
    """
    from src.core.dfm_thresholds import MILLING_RULES

    summary = {
        "sharpCornerCount": 0,
        "sharpCornerRegions": [],
        "hasUndercuts": False,
        "undercutRegions": [],
        "hasDrillHoles": False,
        "drillHoleCount": 0,
        "requiresEdm": False,
        "requiresGrinding": False,
        "minimumFeatureSizeMm": MILLING_RULES.min_internal_radius_mm * 2.0,
        "internalRadiusIssues": 0,
        "cavityDepthIssues": 0,
        "toolAccessAxes": 3,
        "chatterRiskCount": 0,
    }

    for issue in issues:
        cat = issue.get("category", "")

        if cat == "internal_radius":
            summary["internalRadiusIssues"] = issue.get("value", 0)
            summary["minimumFeatureSizeMm"] = issue.get("value", 0) * 2.0

        elif cat == "cavity_depth":
            summary["cavityDepthIssues"] = issue.get("value", 0)

        elif cat == "tool_access":
            summary["toolAccessAxes"] = int(issue.get("value", 3))

        elif cat == "chatter_risk":
            summary["chatterRiskCount"] = issue.get("value", 0)

        elif cat == "sharp_corner":
            summary["sharpCornerCount"] = issue.get("value", 0)
            summary["sharpCornerRegions"] = [issue.get("centroid", [0, 0, 0])]
            summary["requiresEdm"] = summary["sharpCornerCount"] > 20

        elif cat == "hole":
            summary["hasDrillHoles"] = True
            summary["drillHoleCount"] += 1

    return summary


def _generate_cnc_turning_summary(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate legacy summary fields for CNC turning process.

    Extracts key metrics from issues list for backward compatibility.
    """
    # Default values assuming part is turnable
    summary = {
        "isTurnable": True,
        "primaryAxis": "Z",
        "lengthDiameterRatio": 0.0,
        "symmetryDeviation": 0.0,
    }

    for issue in issues:
        cat = issue.get("category", "")

        if cat == "not_turnable":
            summary["isTurnable"] = False
            summary["symmetryDeviation"] = issue.get("value", 0.0)

        elif cat == "ld_ratio":
            summary["lengthDiameterRatio"] = issue.get("value", 0.0)

    return summary


def _compute_dfm_single_body(
    stl_path: str,
    cad_path: str | None,
    cad_ext: str | None,
    body_id: int,
) -> dict[str, Any]:
    """Run DFM analysis for a SINGLE body.

    This function runs in a separate process pool worker.
    If this body crashes, other bodies continue processing.

    Args:
        stl_path: Path to STL file for this body
        cad_path: Optional path to CAD file for B-Rep analysis
        cad_ext: CAD file extension (e.g., "step", "stp")
        body_id: Body identifier for logging

    Returns:
        DFM report dict with process codes (FDM, SLA, CNC_MILL, etc.)
        OR error dict with "error_type" key if analysis fails
    """
    try:
        # Setup worker diagnostics
        try:
            from src.core.worker_wrapper import setup_worker_diagnostics

            worker_id = setup_worker_diagnostics(
                worker_type="dfm_single_body",
                memory_warning_mb=1000.0,
                memory_critical_mb=2000.0,
            )
            logger.info(f"Worker {worker_id} processing body {body_id}")
        except ImportError:
            worker_id = f"dfm_body_{body_id}_{os.getpid()}"
            logger.info(f"Processing body {body_id} (worker diagnostics unavailable)")

        # Read STL bytes
        with open(stl_path, "rb") as fh:  # noqa: PTH123
            stl_bytes = fh.read()

        # Read CAD bytes if available
        cad_bytes = None
        if cad_path and os.path.exists(cad_path):  # noqa: PTH110
            with open(cad_path, "rb") as fh:  # noqa: PTH123
                cad_bytes = fh.read()

        # Collect any garbage from previous tasks before starting — workers live
        # for maxtasksperchild tasks and can accumulate trimesh/numpy residuals.
        import gc

        gc.collect()

        # Run DFM analysis with timeout to prevent indefinite hangs
        # Use 90s timeout to allow complex geometries to complete
        SINGLE_BODY_DFM_TIMEOUT = (  # noqa: N806
            90  # seconds - increased from 50s for complex geometries
        )

        # Platform-specific timeout handling
        if sys.platform != "win32":
            # Unix: use signal.SIGALRM
            import signal

            def _timeout_handler(signum, frame):  # noqa: ARG001
                raise TimeoutError(
                    f"Single-body DFM analysis timed out after {SINGLE_BODY_DFM_TIMEOUT}s"  # noqa: E501
                )

            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(SINGLE_BODY_DFM_TIMEOUT)
            try:
                result = _analyze_single_body(stl_bytes, cad_bytes, cad_ext)
                signal.alarm(0)  # Cancel alarm
            except TimeoutError:
                signal.alarm(0)  # Cancel alarm
                signal.signal(signal.SIGALRM, old_handler)  # Restore old handler
                raise
            except Exception:
                signal.alarm(0)  # Cancel alarm on any exception
                signal.signal(signal.SIGALRM, old_handler)  # Restore old handler
                raise
            finally:
                signal.signal(signal.SIGALRM, old_handler)  # Always restore
        else:
            # Windows: use watchdog thread
            import threading

            result_container = [None]
            exception_container = [None]

            def _run_analysis():
                try:
                    result_container[0] = _analyze_single_body(
                        stl_bytes, cad_bytes, cad_ext  # noqa: F821
                    )
                except Exception as e:
                    exception_container[0] = e

            worker_thread = threading.Thread(target=_run_analysis, daemon=True)
            worker_thread.start()
            worker_thread.join(timeout=SINGLE_BODY_DFM_TIMEOUT)

            if worker_thread.is_alive():
                # Thread is still running - timeout
                raise TimeoutError(
                    f"Single-body DFM analysis timed out after {SINGLE_BODY_DFM_TIMEOUT}s"  # noqa: E501
                )

            if exception_container[0] is not None:
                raise exception_container[0]

            if result_container[0] is None:
                raise TimeoutError(
                    f"Single-body DFM analysis timed out after {SINGLE_BODY_DFM_TIMEOUT}s (no result)"  # noqa: E501
                )

            result = result_container[0]

        logger.info(f"Body {body_id} DFM analysis completed successfully")

        return result

    except Exception as e:
        # Return structured error instead of crashing
        import traceback

        error_result = {
            "error_type": type(e).__name__,
            "error_message": str(e),
            "body_id": body_id,
            "stack_trace": traceback.format_exc(),
            "worker_id": worker_id if "worker_id" in locals() else "unknown",
        }
        logger.error(
            f"Body {body_id} DFM analysis failed: {type(e).__name__}: {e}\n"
            f"Stack trace:\n{traceback.format_exc()}",
            extra={
                "event": "body_dfm_failed",
                "body_id": body_id,
                "error_type": type(e).__name__,
            },
        )
        return error_result
    finally:
        # Free large byte buffers regardless of success/failure — GC collects
        # any remaining trimesh/numpy residuals before the next body runs.
        import gc

        with contextlib.suppress(NameError):
            del stl_bytes
        with contextlib.suppress(NameError):
            del cad_bytes
        gc.collect()


def _aggregate_dfm_reports(
    per_body_reports: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate per-body DFM reports into unified report.

    Sums counts (thin_wall_count, etc.) and takes worst severity across bodies.
    Returns a single report dict with the same structure as _analyze_single_body.
    """
    if not per_body_reports:
        return {}

    # Aggregate by process code
    aggregated: dict[str, Any] = {}

    # Get all process codes from first report (all bodies should have same processes)
    first_report = next(iter(per_body_reports.values()))
    process_codes = set(first_report.keys()) - {"CNC"}  # Skip legacy CNC key

    for process_code in process_codes:
        if process_code.startswith("CNC"):
            continue  # Handle CNC separately below

        # Collect all reports for this process across bodies
        process_reports = [
            report.get(process_code, {})
            for report in per_body_reports.values()
            if process_code in report
        ]

        if not process_reports:
            continue

        # Start with first report as template
        base_report = process_reports[0].copy()
        all_issues = list(base_report.get("issues", []))

        # Aggregate issues across bodies (concatenate with limits)
        for report in process_reports[1:]:
            issues = report.get("issues", [])
            all_issues.extend(issues)

        # Limit total issues to prevent overwhelming response
        base_report["issues"] = all_issues[:200]

        # Aggregate legacy summary fields
        base_report["thinWallCount"] = sum(
            r.get("thinWallCount", 0) for r in process_reports
        )
        base_report["thinWallRegions"] = [
            region
            for r in process_reports
            for region in r.get("thinWallRegions", [])[:50]
        ][:100]
        base_report["overhangFaceCount"] = sum(
            r.get("overhangFaceCount", 0) for r in process_reports
        )
        base_report["overhangAreaCm2"] = sum(
            r.get("overhangAreaCm2", 0.0) for r in process_reports
        )
        base_report["overhangRegions"] = [
            region
            for r in process_reports
            for region in r.get("overhangRegions", [])[:50]
        ][:100]
        base_report["supportRequired"] = any(
            r.get("supportRequired", False) for r in process_reports
        )
        base_report["estimatedSupportVolumeCm3"] = sum(
            r.get("estimatedSupportVolumeCm3", 0.0) or 0.0 for r in process_reports
        )
        base_report["smallDetailCount"] = sum(
            r.get("smallDetailCount", 0) for r in process_reports
        )

        # Process-specific fields
        if process_code in ("SLA", "SLA_DLP"):
            base_report["resinTrappingRisk"] = any(
                r.get("resinTrappingRisk", False) for r in process_reports
            )
            base_report["resinTrappingRegions"] = [
                region
                for r in process_reports
                for region in r.get("resinTrappingRegions", [])[:10]
            ][:50]
            base_report["suctionRisk"] = any(
                r.get("suctionRisk", False) for r in process_reports
            )
            base_report["suctionRegions"] = [
                region
                for r in process_reports
                for region in r.get("suctionRegions", [])[:5]
            ][:10]
            base_report["hollowRegions"] = [
                region
                for r in process_reports
                for region in r.get("hollowRegions", [])[:10]
            ][:50]

        aggregated[process_code] = base_report

    # Aggregate CNC_MILL (if present)
    cnc_mill_reports = [
        report.get("CNC_MILL", {})
        for report in per_body_reports.values()
        if "CNC_MILL" in report
    ]
    if cnc_mill_reports:
        base_cnc = cnc_mill_reports[0].copy()
        all_cnc_issues = list(base_cnc.get("issues", []))

        for report in cnc_mill_reports[1:]:
            all_cnc_issues.extend(report.get("issues", []))

        base_cnc["issues"] = all_cnc_issues[:200]
        base_cnc["sharpCornerCount"] = sum(
            r.get("sharpCornerCount", 0) for r in cnc_mill_reports
        )
        base_cnc["sharpCornerRegions"] = [
            region
            for r in cnc_mill_reports
            for region in r.get("sharpCornerRegions", [])[:25]
        ][:50]
        base_cnc["hasDrillHoles"] = any(
            r.get("hasDrillHoles", False) for r in cnc_mill_reports
        )
        base_cnc["drillHoleCount"] = sum(
            r.get("drillHoleCount", 0) for r in cnc_mill_reports
        )
        base_cnc["requiresEdm"] = any(
            r.get("requiresEdm", False) for r in cnc_mill_reports
        )
        base_cnc["internalRadiusIssues"] = sum(
            r.get("internalRadiusIssues", 0) for r in cnc_mill_reports
        )
        base_cnc["cavityDepthIssues"] = sum(
            r.get("cavityDepthIssues", 0) for r in cnc_mill_reports
        )
        base_cnc["chatterRiskCount"] = sum(
            r.get("chatterRiskCount", 0) for r in cnc_mill_reports
        )

        aggregated["CNC_MILL"] = base_cnc
        # Legacy CNC key
        aggregated["CNC"] = dict(base_cnc)
        aggregated["CNC"]["reportType"] = "CNC"

    # Aggregate CNC_TURN (if present)
    cnc_turn_reports = [
        report.get("CNC_TURN", {})
        for report in per_body_reports.values()
        if "CNC_TURN" in report
    ]
    if cnc_turn_reports:
        base_turn = cnc_turn_reports[0].copy()
        all_turn_issues = list(base_turn.get("issues", []))

        for report in cnc_turn_reports[1:]:
            all_turn_issues.extend(report.get("issues", []))

        base_turn["issues"] = all_turn_issues[:200]
        # For turning, if ANY body is not turnable, the assembly is not turnable
        base_turn["isTurnable"] = all(
            r.get("isTurnable", False) for r in cnc_turn_reports
        )
        # Use worst L/D ratio across bodies
        base_turn["lengthDiameterRatio"] = max(
            (
                r.get("lengthDiameterRatio", 0.0)
                for r in cnc_turn_reports
                if r.get("lengthDiameterRatio")
            ),
            default=0.0,
        )
        base_turn["symmetryDeviation"] = max(
            (r.get("symmetryDeviation", 0.0) for r in cnc_turn_reports),
            default=0.0,
        )

        aggregated["CNC_TURN"] = base_turn

    return aggregated


def _compute_dfm_worker(
    stl_bytes: bytes | dict[int, bytes],
    cad_bytes: bytes | None = None,
    cad_extension: str | None = None,
) -> dict[str, Any]:
    """Phase 3 worker: runs DFM analysis for all manufacturing processes.

    Args:
        stl_bytes: EITHER single bytes (legacy) OR dict mapping body_id → stl_bytes (multi-body)

    Returns a dict keyed by process code ("FDM", "SLA", "SLS", "MJF", "MJ",
    "BJ", "DMLS", "CNC_MILL", "CNC_TURN", plus legacy "CNC").
    For multi-body input, aggregates reports across all bodies.

    Each value contains:
      - ``issues``: list of DfmIssue-style dicts with ``faceIndices`` for overlay
        GLB generation.
      - Legacy summary fields for backward-compat with existing C# consumers.

    Runs in a separate process via ProcessPoolExecutor.
    """  # noqa: E501
    # Handle multi-body input
    is_multi_body = isinstance(stl_bytes, dict)

    if is_multi_body:
        # Run DFM PER BODY in parallel (3-5x speedup for 13-body files)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        per_body_reports: dict[int, dict[str, Any]] = {}
        body_count = len(stl_bytes)
        body_workers = max(1, min(settings.GEOMETRY_DFM_BODY_WORKERS, body_count, 4))

        logger.info(
            "Parallel DFM analysis for %d bodies using %d body workers",
            body_count,
            body_workers,
            extra={
                "event": "dfm_parallel_start",
                "body_count": body_count,
                "body_workers": body_workers,
            },
        )

        # Use ThreadPoolExecutor for parallel DFM analysis
        # Each body analysis is independent, so we can process them concurrently
        # Max 4 workers to balance parallelism with resource usage
        # Per-body timeout prevents indefinite hangs (90s for complex geometries)
        PER_BODY_TIMEOUT = 90  # noqa: N806

        with ThreadPoolExecutor(max_workers=body_workers) as executor:
            # Submit all bodies for analysis
            futures = {
                executor.submit(
                    _analyze_single_body, body_stl_bytes, cad_bytes, cad_extension
                ): body_id
                for body_id, body_stl_bytes in stl_bytes.items()
            }

            logger.info(
                "Submitted %d bodies for parallel DFM analysis",
                len(futures),
                extra={"event": "dfm_submitted", "task_count": len(futures)},
            )

            # Collect results as they complete
            for future in as_completed(futures):
                body_id = futures[future]
                try:
                    # Add timeout to prevent indefinite hangs inside the worker
                    body_report = future.result(timeout=PER_BODY_TIMEOUT)
                    if body_report:
                        per_body_reports[body_id] = body_report
                        logger.info(
                            "✓ DFM analysis complete for body %d/%d",
                            body_id + 1,
                            body_count,
                            extra={"event": "dfm_body_complete", "body_id": body_id},
                        )
                    else:
                        logger.warning(
                            "✗ DFM analysis returned empty report for body %d/%d",
                            body_id + 1,
                            body_count,
                            extra={"event": "dfm_body_empty", "body_id": body_id},
                        )
                except concurrent.futures.TimeoutError:
                    # Explicitly handle timeout to continue processing other bodies
                    logger.warning(
                        "✗ DFM analysis timed out for body %d/%d after %ds\n"
                        "Body STL size: %d bytes\n"
                        "CAD file: %s (%s)\n"
                        "Continuing with other bodies...",
                        body_id + 1,
                        body_count,
                        PER_BODY_TIMEOUT,
                        len(stl_bytes[body_id]) if body_id in stl_bytes else "unknown",
                        "STEP/IGES" if cad_bytes else "N/A",
                        cad_extension if cad_extension else "N/A",
                        extra={"event": "dfm_body_timeout", "body_id": body_id},
                    )
                except Exception as e:
                    logger.warning(
                        "✗ DFM analysis failed for body %d/%d: %s\n"
                        "Body STL size: %d bytes\n"
                        "CAD file: %s (%s)\n"
                        "Error type: %s\n"
                        "Stack trace will follow...",
                        body_id + 1,
                        body_count,
                        str(e),
                        len(stl_bytes[body_id]),
                        "STEP/IGES" if cad_bytes else "N/A",
                        cad_extension if cad_extension else "N/A",
                        type(e).__name__,
                        extra={"event": "dfm_body_failed", "body_id": body_id},
                        exc_info=True,  # Include full stack trace
                    )

        if not per_body_reports:
            logger.warning("DFM analysis failed for all bodies")
            return {}

        # Aggregate reports across bodies
        logger.info(
            "Aggregating DFM reports from %d bodies",
            len(per_body_reports),
            extra={"event": "dfm_aggregate_start"},
        )
        aggregated = _aggregate_dfm_reports(per_body_reports)

        return {
            **aggregated,
            "per_body_reports": per_body_reports,  # Include for debugging
            "body_count": len(stl_bytes),
        }
    # Legacy single-body path
    return _analyze_single_body(stl_bytes, cad_bytes, cad_extension)


def _generate_overlays_worker(
    stl_bytes: bytes | dict[int, bytes],
    reports: dict[str, Any],
) -> dict[str, bytes]:
    """Phase 3 worker: generate overlay GLB bytes for each process+category.

    Returns a dict keyed by ``"{PROCESS}__{category}"`` (double-underscore avoids
    collisions with process codes like ``CNC_MILL``) mapping to GLB bytes.

    Runs in a separate process via ProcessPoolExecutor.

    Args:
        stl_bytes: EITHER single bytes (legacy) OR dict mapping body_id → stl_bytes (multi-body).
                   Tessellated mesh(es) in STL format (Z-up, mm).
        reports:   DFM reports dict from ``_compute_dfm_worker``.
    """  # noqa: E501
    try:
        from src.core.overlay_generator import (
            generate_multi_body_overlay_glb,
            generate_overlay_glb,
            generate_support_tower_overlay_glb,
        )
    except ImportError:
        return {}

    result: dict[str, bytes] = {}

    # The viewer GLB is always in mm for all formats — _export_glb_worker converts
    # cascadio's meter-scale output to mm before upload. No unit conversion needed here.

    try:
        # Handle multi-body vs single-body input
        is_multi_body = isinstance(stl_bytes, dict)

        if is_multi_body:
            # Load each body separately (pre-split from metrics phase)
            mesh_list = []
            for body_id, body_stl_bytes in stl_bytes.items():
                try:
                    body_mesh = trimesh.load(
                        io.BytesIO(body_stl_bytes), file_type="stl", force="mesh"
                    )
                    if (
                        isinstance(body_mesh, trimesh.Trimesh)
                        and len(body_mesh.vertices) > 0
                    ):
                        mesh_list.append(body_mesh)
                except Exception as e:
                    logger.warning(
                        "Failed to load body %d for overlay: %s",
                        body_id,
                        e,
                    )

            if not mesh_list:
                return result

            # Concatenate for center calculation and single-body overlays
            mesh = (
                trimesh.util.concatenate(mesh_list)
                if len(mesh_list) > 1
                else mesh_list[0]
            )
            center = mesh.center_mass

            # Multi-body overlay - use PRE-SPLIT bodies (no re-splitting!)
            if len(mesh_list) > 1:
                try:
                    multi_body_glb = generate_multi_body_overlay_glb(mesh_list, center)
                    if multi_body_glb:
                        result["GENERAL__multi_body"] = multi_body_glb
                except Exception as _mb_err:
                    logger.debug("Multi-body overlay GLB failed: %s", _mb_err)
        else:
            # Legacy single-body path
            mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
            if not isinstance(mesh, trimesh.Trimesh) or len(mesh.vertices) == 0:
                return result

            center = mesh.center_mass

            # Multi-body overlay: split the mesh into connected components and
            # tint each body a distinct colour so the user can see the separation.
            # NOTE: This is expensive but necessary for legacy single-body input
            # where we don't have the original body separation preserved.
            bodies = mesh.split(only_watertight=False)
            if len(bodies) > 1:
                try:
                    multi_body_glb = generate_multi_body_overlay_glb(bodies, center)
                    if multi_body_glb:
                        result["GENERAL__multi_body"] = multi_body_glb
                except Exception as _mb_err:
                    logger.debug("Multi-body overlay GLB failed: %s", _mb_err)

        # OPTIMIZATION: Only process actual DFM reports, skip quality metrics and deferred reports  # noqa: E501
        from src.core.dfm_thresholds import PRINTING_RULES

        # Valid process codes that can have overlay visualizations
        process_keys = set(PRINTING_RULES.keys()) | {"CNC_MILL", "CNC", "CNC_TURN"}

        logger.info(
            "Overlay worker: report_keys=%s valid_process_keys=%s",
            list(reports.keys()),
            sorted(process_keys),
        )

        for process_code, report in reports.items():
            # Skip if not a process report (e.g., "quality", "hollow_regions")
            if process_code not in process_keys:
                logger.warning(
                    "Overlay worker: skipping unknown process_code=%r (not in valid process keys)",  # noqa: E501
                    process_code,
                )
                continue

            # Skip CNC turning (overlays not yet supported)
            if process_code == "CNC_TURN":
                continue

            # Skip if report is two-phase deferred (no actual analysis done yet)
            if report.get("twoPhaseDeferred", False):
                continue

            issues = report.get("issues", [])
            issues_with_faces = sum(1 for i in issues if i.get("faceIndices"))
            logger.info(
                "Overlay worker: process=%s total_issues=%d issues_with_face_indices=%d",  # noqa: E501
                process_code,
                len(issues),
                issues_with_faces,
            )
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

            # Support-tower overlay: union of all overhang face indices for this process.  # noqa: E501
            # Generates {process}__overhang_support alongside the regular overhang overlay.  # noqa: E501
            all_overhang_indices: list[int] = []
            for issue in issues:
                if issue.get("category") == "overhang":
                    all_overhang_indices.extend(issue.get("faceIndices", []))
            if all_overhang_indices:
                support_key = f"{process_code}__overhang_support"
                try:
                    support_glb = generate_support_tower_overlay_glb(
                        mesh,
                        list(set(all_overhang_indices)),
                        center,
                    )
                    if support_glb:
                        result[support_key] = support_glb
                except Exception as _st_err:
                    logger.debug(
                        "Support-tower overlay GLB failed for %s: %s",
                        process_code,
                        _st_err,
                    )

    except Exception as exc:
        logger.warning("_generate_overlays_worker failed: %s", exc)

    return result


def _render_large_preview_worker(stl_bytes: bytes) -> dict[str, bytes | None]:
    """Phase 2 worker: generates 7 preview images (6 ortho + 1 iso) + 1200px ISO in PARALLEL. Runs in a separate process."""  # noqa: E501
    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            return {}
        if len(mesh.faces) == 0:
            logger.warning("Mesh has no polygon faces — skipping preview generation")
            return {}
        return _generate_preview_images_parallel(mesh)  # Use parallel version
    except Exception as e:
        logger.warning(f"_render_large_preview_worker failed: {e}")
        return {}


def _render_preview_from_glb_worker(glb_bytes: bytes) -> dict[str, bytes | None]:
    """Phase 2 worker: generates 7 preview images (6 ortho + 1 iso) + 1200px ISO from GLB.
    Uses trimesh to load GLB, then converts to PyVista for rendering.

    Implements progressive rendering: if one view fails, returns partial results
    instead of discarding all successful views.
    """  # noqa: E501
    import trimesh

    # Return empty result structure for failures
    empty_result = {f"{view}_small": None for view in ORTHO_VIEWS}
    empty_result["thumbnail_small"] = None
    empty_result["thumbnail_large"] = None

    try:
        # Load GLB using trimesh (supports glTF/GLB)
        scene_data = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")
    except Exception as e:
        logger.warning(f"Failed to load GLB for preview generation: {e}")
        return empty_result

    try:
        # Extract mesh from scene (handle both Scene and Trimesh objects)
        if isinstance(scene_data, trimesh.Scene):
            if len(scene_data.geometry) == 0:
                logger.warning("Empty scene - no geometries found")
                return empty_result
            # For multi-body files, use the largest mesh for preview generation
            # This preserves original zoom level and colors for single-body files
            geometries = list(scene_data.geometry.values())
            if len(geometries) == 1:
                mesh = geometries[0]
            else:
                # Use the largest mesh by vertex count for preview
                # This gives a good representation without changing bounds/zoom
                mesh = max(
                    geometries,
                    key=lambda g: len(g.vertices)
                    if isinstance(g, trimesh.Trimesh)
                    else 0,
                )
                logger.info(
                    f"Using largest mesh for preview ({len(geometries)} total bodies)"
                )
        else:
            mesh = scene_data

        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.vertices) == 0:
            logger.warning("Invalid mesh data")
            return empty_result

        # Validate mesh before rendering
        vertices = mesh.vertices
        faces = mesh.faces
        if len(vertices) == 0:
            logger.warning("PyVista mesh creation failed - no points")
            return empty_result
        if len(faces) == 0:
            logger.warning("GLB has no polygon faces — skipping preview generation")
            return empty_result

        logger.info(
            f"Preview mesh loaded: {len(vertices)} vertices, {len(faces)} faces"
        )

        # Generate previews in PARALLEL with progressive error handling per-view
        return _generate_preview_images_parallel(mesh)

    except Exception as e:
        logger.warning(
            f"Preview generation failed during rendering phase: {e}", exc_info=True
        )
        # Return empty result structure but log that it's a partial failure
        return empty_result


# ---------------------------------------------------------------------------
# Path-based wrappers for ProcessPoolExecutor calls
#
# Each worker receives a file path (a tiny string) through the multiprocessing
# pipe instead of hundreds of MB of bytes. The worker reads the file directly
# from disk, keeping pipe traffic negligible and avoiding OOM kills.
# ---------------------------------------------------------------------------


def _render_thumbnail_worker(glb_path: str) -> bytes | None:
    """Renders a 256px isometric thumbnail from GLB.

    GLB-first pipeline: all formats produce GLB in Phase 1, Phase 2 always uses GLB.
    """
    if not os.path.exists(glb_path):  # noqa: PTH110
        logger.warning(f"GLB thumbnail source not found: {glb_path}")
        return None

    # Use cached GLB to avoid redundant disk I/O
    cached_glb = _get_cached_glb(glb_path)
    if cached_glb:
        # Export cached GLB to bytes for the worker

        glb_bytes = (
            cached_glb.export(file_type="glb")
            if hasattr(cached_glb, "export")
            else None
        )
        if glb_bytes:
            return _render_thumbnail_from_glb_worker(glb_bytes)

    # Fallback: load from disk
    with open(glb_path, "rb") as fh:  # noqa: PTH123
        return _render_thumbnail_from_glb_worker(fh.read())


def _export_glb_from_paths(cad_glb_path: str, file_ext: str = "") -> bytes | None:
    """Reads CAD GLB from disk and delegates to _export_glb_worker.

    GLB-first pipeline: all formats produce GLB in Phase 1, Phase 2 always uses GLB.

    Args:
        cad_glb_path: Path to cad.glb file written by Phase 1
        file_ext: Original file extension (with dot, e.g., ".step", ".stl")
    """
    if not os.path.exists(cad_glb_path):  # noqa: PTH110
        logger.warning(f"GLB export source not found: {cad_glb_path}")
        return None

    # Use cached GLB to avoid redundant disk I/O
    cached_glb = _get_cached_glb(cad_glb_path)
    if cached_glb:
        # Export cached GLB to bytes for the worker
        glb_bytes = (
            cached_glb.export(file_type="glb")
            if hasattr(cached_glb, "export")
            else None
        )
        if glb_bytes:
            return _export_glb_worker(glb_bytes, file_ext)

    # Fallback: load from disk
    with open(cad_glb_path, "rb") as fh:  # noqa: PTH123
        return _export_glb_worker(fh.read(), file_ext)


def _render_preview_worker(glb_path: str) -> dict[str, bytes | None]:
    """Generates 7 preview images (6 ortho + 1 iso) + 1200px ISO from GLB.

    GLB-first pipeline: all formats produce GLB in Phase 1, Phase 2 always uses GLB.
    """
    if not os.path.exists(glb_path):  # noqa: PTH110
        logger.warning(f"GLB preview source not found: {glb_path}")
        return {}

    # Use cached GLB to avoid redundant disk I/O
    cached_glb = _get_cached_glb(glb_path)
    if cached_glb:
        # Export cached GLB to bytes for the worker

        glb_bytes = (
            cached_glb.export(file_type="glb")
            if hasattr(cached_glb, "export")
            else None
        )
        if glb_bytes:
            return _render_preview_from_glb_worker(glb_bytes)

    # Fallback: load from disk
    with open(glb_path, "rb") as fh:  # noqa: PTH123
        return _render_preview_from_glb_worker(fh.read())


def _compute_dfm_from_paths(
    stl_paths: dict[int, str] | None,
    cad_path: str | None,
    cad_ext: str | None,
    glb_path: str | None = None,
) -> dict:
    """Reads per-body STLs, GLB, and optional CAD file from disk, delegates to _compute_dfm_worker.

    For multi-body CAD files (STEP/IGES) with GLB:
    - Extracts per-body meshes from the GLB scene graph
    - Runs DFM analysis on each body separately
    - Aggregates results across all bodies

    For single-body CAD files:
    - Uses the CAD file directly with tessellation on-demand

    For pure STL/OBJ uploads:
    - Uses pre-exported STL files
    """  # noqa: E501
    stl_bytes_dict: dict[int, bytes] = {}

    # Priority 1: Use GLB for multi-body mesh extraction (CAD files with cascadio)
    if glb_path and os.path.exists(glb_path) and not stl_paths:  # noqa: PTH110
        logger.info("Extracting per-body meshes from GLB for multi-body DFM analysis")
        try:
            import io

            import trimesh

            with open(glb_path, "rb") as fh:  # noqa: PTH123
                glb_bytes = fh.read()

            # Load GLB scene with trimesh
            scene_data = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")

            # Extract per-body meshes from scene graph
            if isinstance(scene_data, trimesh.Scene):
                body_id = 0
                for node_name, geometry in scene_data.geometry.items():
                    if (
                        isinstance(geometry, trimesh.Trimesh)
                        and len(geometry.vertices) > 0
                    ):
                        # Export each body mesh as STL bytes for DFM analysis
                        body_stl_bytes = geometry.export(file_type="stl")
                        stl_bytes_dict[body_id] = body_stl_bytes
                        logger.info(
                            "Extracted body %d (%s): %d vertices, %d faces, %d bytes",
                            body_id,
                            node_name,
                            len(geometry.vertices),
                            len(geometry.faces),
                            len(body_stl_bytes),
                        )
                        body_id += 1

            if stl_bytes_dict:
                logger.info(
                    "Extracted %d bodies from GLB for DFM analysis",
                    len(stl_bytes_dict),
                    extra={
                        "event": "multibody_extract",
                        "body_count": len(stl_bytes_dict),
                    },
                )
            else:
                logger.warning("No valid bodies found in GLB scene")

        except Exception as e:
            logger.warning(
                f"Failed to extract bodies from GLB: {e}",
                exc_info=True,
                extra={"event": "glb_extraction_failed"},
            )

    # Priority 2: Use pre-exported per-body STL files (legacy path)
    if not stl_bytes_dict and stl_paths:
        for idx, path in stl_paths.items():
            with open(path, "rb") as fh:  # noqa: PTH123
                stl_bytes_dict[idx] = fh.read()

    # Load CAD file for B-Rep analysis (fallback or single-body)
    cad_bytes: bytes | None = None
    if cad_path and os.path.exists(cad_path):  # noqa: PTH110
        with open(cad_path, "rb") as fh:  # noqa: PTH123
            cad_bytes = fh.read()

    # Multi-body: Run DFM per body using extracted meshes
    if stl_bytes_dict:
        return _compute_dfm_worker(stl_bytes_dict, cad_bytes, cad_ext)

    # Single-body: Use CAD file directly with tessellation
    if cad_bytes:
        return _compute_dfm_worker(cad_bytes, cad_bytes, cad_ext)

    # No data available
    logger.warning("No STL, GLB, or CAD data available for DFM analysis")
    return {}


def _generate_overlays_from_paths(
    glb_path: str,
    reports: dict,
) -> dict[str, str]:
    """Extract mesh data from GLB, generate overlays, and return GCS paths.

    This is a synchronous wrapper that only generates the GLB bytes.
    The actual upload to GCS happens in the async upload_consumer to avoid
    blocking the process pool with HTTP requests.

    Returns:
        Dict mapping "{PROCESS}__{category}" → GLB bytes (to be uploaded later).

    Note: This returns GLB bytes, not GCS paths. The caller (upload_consumer)
    is responsible for uploading these bytes to GCS and converting to paths.
    """

    # Use cached GLB to avoid redundant disk I/O
    scene_data = _get_cached_glb(glb_path)
    if scene_data is None:
        logger.warning("Failed to load GLB for overlay generation: %s", glb_path)
        return {}

    # Extract per-body meshes for multi-body files
    mesh_bytes_dict: dict[int, bytes] = {}
    if isinstance(scene_data, trimesh.Scene):
        for idx, (_name, geom) in enumerate(scene_data.geometry.items()):
            if isinstance(geom, trimesh.Trimesh) and len(geom.vertices) > 0:
                try:
                    mesh_bytes_dict[idx] = geom.export(file_type="stl")
                except Exception as e:
                    logger.warning("Failed to export body %d as STL: %s", idx, e)
    elif isinstance(scene_data, trimesh.Trimesh):
        try:
            mesh_bytes_dict[0] = scene_data.export(file_type="stl")
        except Exception as e:
            logger.warning("Failed to export single-body mesh as STL: %s", e)

    if not mesh_bytes_dict:
        logger.warning(
            "No valid meshes found in GLB for overlay generation: %s", glb_path
        )
        return {}

    logger.info(
        "Extracted %d body(es) from GLB for overlay generation", len(mesh_bytes_dict)
    )

    # Generate overlay GLBs (returns dict[str, bytes])
    return _generate_overlays_worker(mesh_bytes_dict, reports)

    # Return GLB bytes - caller will upload to GCS


class GeometryProcessor:
    def __init__(self, enable_diagnostics: bool = True) -> None:
        _cpu = os.cpu_count() or 2
        # Limit workers to avoid concurrent OOM on large files.
        # Each worker loads hundreds of MB of STL data from disk.
        _workers = settings.GEOMETRY_MAIN_WORKERS or max(2, min(4, _cpu // 2))
        _workers = max(1, min(_workers, 4))
        _dfm_workers = max(1, min(settings.GEOMETRY_DFM_WORKERS, 2))

        # Rendering executor: recycles workers after 5 jobs so trimesh/numpy
        # pages accumulated during thumbnail/GLB/preview work are released back
        # to the OS.  Workers are cheap to restart (spawn context) and the
        # initializer warm-up cost is amortised across the 5 tasks.
        #
        # Worker recycling strategy by Python version:
        #   Python ≥ 3.11: ProcessPoolExecutor supports max_tasks_per_child →
        #                   use DiagnosticExecutor (rich crash diagnostics).
        #   Python < 3.11: ProcessPoolExecutor lacks max_tasks_per_child →
        #                   use PoolExecutorWrapper (multiprocessing.Pool) which
        #                   supports maxtasksperchild on all Python versions.
        #                   This is the primary path on the current 3.10 runtime.
        if enable_diagnostics and sys.version_info >= (3, 11):
            try:
                from src.core.worker_wrapper import DiagnosticExecutor

                self.executor = DiagnosticExecutor(
                    max_workers=_workers,
                    mp_context=multiprocessing.get_context("spawn"),
                    enable_diagnostics=True,
                    memory_warning_mb=1000.0,
                    memory_critical_mb=2000.0,
                    max_tasks_per_child=5,
                )
                logger.info(
                    "GeometryProcessor initialized with DiagnosticExecutor "
                    "(max_tasks_per_child=5)"
                )
            except ImportError:
                logger.warning(
                    "DiagnosticExecutor unavailable, falling back to PoolExecutorWrapper"  # noqa: E501
                )
                self.executor = PoolExecutorWrapper(
                    processes=_workers,
                    maxtasksperchild=5,
                )
        else:
            # Python 3.10 or diagnostics disabled: PoolExecutorWrapper provides
            # maxtasksperchild recycling on all Python versions.
            self.executor = PoolExecutorWrapper(
                processes=_workers,
                maxtasksperchild=5,
            )
            if enable_diagnostics:
                logger.info(
                    "GeometryProcessor initialized with PoolExecutorWrapper "
                    "(maxtasksperchild=5, Python 3.10 — DiagnosticExecutor "
                    "requires Python 3.11+ for worker recycling)"
                )

        # Separate executor for DFM — isolated so DFM OOM crashes don't corrupt
        # the rendering pipeline. DFM is the most memory-intensive task (loads
        # large STL mesh + OCC B-Rep for STEP files). processes=2 enables
        # parallelism while maxtasksperchild bounds memory via worker recycling.
        #
        # maxtasksperchild=2: worker is recycled after 2 jobs to aggressively
        # bound RSS accumulation. Even with explicit gc.collect() between bodies,
        # trimesh/numpy pages may not be released back to the OS immediately;
        # recycling the process guarantees a clean slate. The startup cost is
        # amortised by the initializer pre-importing heavy modules.
        #
        # initializer=_dfm_worker_initializer: pre-imports OCC/trimesh/numpy/scipy
        # once per worker spawn, amortising the ~1-3 s cold-start cost.
        self.dfm_executor = PoolExecutorWrapper(
            processes=_dfm_workers,
            maxtasksperchild=2,
            initializer=_dfm_worker_initializer,
        )

    def shutdown(self, timeout: int = 30) -> None:  # noqa: ARG002
        """Shutdown executors gracefully with timeout.

        Args:
            timeout: Maximum seconds to wait for each executor to shut down
        """
        # Main executor shutdown - use shutdown() method
        if hasattr(self, "executor"):
            try:
                self.executor.shutdown(wait=True)
            except Exception as e:
                logger.warning(f"Executor shutdown failed: {e}")
                try:
                    self.executor._pool.terminate()
                    self.executor._pool.join()
                except Exception:
                    pass

        # DFM executor shutdown
        if hasattr(self, "dfm_executor"):
            try:
                self.dfm_executor.shutdown(wait=True)
            except Exception as e:
                logger.warning(f"DFM executor shutdown failed: {e}")
                try:
                    self.dfm_executor._pool.terminate()
                    self.dfm_executor._pool.join()
                except Exception:
                    pass

    def _rebuild_dfm_executor(self) -> None:
        """Rebuild the DFM executor after a crash."""
        logger.info("Rebuilding DFM executor after crash")

        try:
            self.dfm_executor.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"DFM executor shutdown failed during rebuild: {e}")

        self.dfm_executor = PoolExecutorWrapper(
            processes=max(1, min(settings.GEOMETRY_DFM_WORKERS, 2)),
            maxtasksperchild=2,
            initializer=_dfm_worker_initializer,
        )

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
                        # Use finer mesh for Phase 2 GLB quality (0.05mm) but with geometry healing  # noqa: E501
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
                    if (
                        not isinstance(dumped_mesh2, trimesh.Trimesh)
                        or len(dumped_mesh2.vertices) == 0
                    ):
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


# ============================================================================
# Compatibility wrappers for legacy test API
# These wrap GeometryProcessor for backward compatibility with older test files
# ============================================================================

_legacy_processor: GeometryProcessor | None = None


def _get_legacy_processor() -> GeometryProcessor:
    """Get or create the legacy processor instance."""
    global _legacy_processor
    if _legacy_processor is None:
        _legacy_processor = GeometryProcessor(enable_diagnostics=False)
    return _legacy_processor


def compute_dfm_analysis_for_stl(
    stl_bytes: bytes,
    timeout_seconds: float = 30,  # noqa: ARG001
) -> dict[str, Any] | None:
    """Legacy compatibility wrapper for DFM analysis.

    Runs _analyze_single_body and returns a structured result dict.
    """
    import time

    start_time = time.monotonic()

    try:
        body_result = _analyze_single_body(stl_bytes)
        elapsed = time.monotonic() - start_time

        # Convert the per-process report dict to a list of report objects
        reports = []
        for process_code, report in body_result.items():
            if isinstance(report, dict) and process_code not in (
                "quality",
                "hollow_regions",
            ):
                reports.append({"body_id": 0, "process": process_code, **report})

        return {
            "success": True,
            "duration_seconds": elapsed,
            "reports": reports,
        }
    except Exception as e:
        elapsed = time.monotonic() - start_time
        return {
            "success": False,
            "duration_seconds": elapsed,
            "error": str(e),
        }


def compute_multi_body_dfm_analysis(
    stl_bytes: bytes,
    timeout_seconds: float = 30,
) -> dict[str, Any] | None:
    """Legacy compatibility wrapper for multi-body DFM analysis."""
    return compute_dfm_analysis_for_stl(stl_bytes, timeout_seconds)


def load_cascadio_geometry(
    file_bytes: bytes,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Load CAD file via cascadio and return structured result.

    Public wrapper around _load_cad_with_cascadio_isolated that returns
    a dict compatible with integration test expectations.

    Returns:
        Dict with keys:
          success: bool
          bodies: list of {"mesh": bytes} (per-body GLB bytes)
          body_count: int
          body_names: list[str]
          error: str (only present on failure)
    """
    try:
        meshes, _combined_glb, body_count, body_names = (
            _load_cad_with_cascadio_isolated(file_bytes, timeout_seconds)
        )
        bodies = [
            {"mesh": mesh.export(file_type="glb"), "name": name}
            for mesh, name in zip(meshes, body_names, strict=False)
        ]
        return {
            "success": True,
            "bodies": bodies,
            "body_count": body_count,
            "body_names": body_names,
        }
    except Exception as e:
        logger.warning("load_cascadio_geometry failed: %s", e)
        return {
            "success": False,
            "error": str(e),
            "bodies": [],
            "body_count": 0,
            "body_names": [],
        }
