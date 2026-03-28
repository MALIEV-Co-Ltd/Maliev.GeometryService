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


def _compute_thin_wall_analysis(mesh: trimesh.Trimesh) -> tuple[int, list[list[float]]]:
    """
    Detect thin-wall regions where wall thickness is below threshold.
    Uses mesh face adjacencies to estimate local wall thickness.
    Returns (thin_wall_count, thin_wall_centroids).
    """
    thin_wall_count = 0
    thin_wall_centroids: list[list[float]] = []

    try:
        if len(mesh.faces) < 3:
            return 0, []

        face_adjacency = mesh.face_adjacency
        unique_edges: set[tuple[int, int]] = set()

        for i, j in face_adjacency:
            fi = mesh.faces[i]
            fj = mesh.faces[j]
            for vi in fi:
                for vj in fj:
                    if vi != vj:
                        edge = tuple(sorted([int(vi), int(vj)]))
                        unique_edges.add(edge)

        edge_lengths: list[tuple[float, tuple[int, int], np.ndarray]] = []
        vertices = mesh.vertices

        for edge in unique_edges:
            v0, v1 = vertices[edge[0]], vertices[edge[1]]
            length = float(np.linalg.norm(v1 - v0))
            mid = (v0 + v1) / 2
            edge_lengths.append((length, edge, mid))

        edge_lengths.sort(key=lambda x: x[0])

        thin_edges = [e for e in edge_lengths if e[0] < THIN_WALL_THRESHOLD_MM]

        if not thin_edges:
            return 0, []

        processed_faces: set[int] = set()
        for length, edge, mid in thin_edges:
            for idx, face in enumerate(mesh.faces):
                edge_verts = set(int(v) for v in face)
                if int(edge[0]) in edge_verts and int(edge[1]) in edge_verts:
                    if idx not in processed_faces:
                        processed_faces.add(idx)
                        thin_wall_centroids.append(
                            [float(mid[0]), float(mid[1]), float(mid[2])]
                        )

        thin_wall_count = len(processed_faces)

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
            # Use 2.0mm mesh for maximum reliability on problematic STEP files.
            # DFM metrics (volume, bounding box, watertight check) don't need fine resolution.
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
        # subprocess.  If the thread fires before gmsh returns, we terminate the process.
        result = {}

        def _gmsh_work():
            try:
                gmsh.initialize()
                gmsh.option.setNumber("General.Verbosity", 0)
                gmsh.open(file_path)
                # Use 2.0mm mesh for maximum reliability on problematic STEP files.
                # DFM metrics don't need fine resolution.
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
) -> tuple[trimesh.Trimesh, bytes]:
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
                tol_linear=0.1,
                tol_angular=0.5,
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
                mesh = trimesh.util.concatenate(meshes)
            elif isinstance(loaded, trimesh.Trimesh):
                mesh = loaded
            else:
                raise ValueError(f"cascadio produced unexpected type: {type(loaded)}")
            if len(mesh.vertices) == 0:
                raise ValueError(f"cascadio produced empty mesh for {file_path}")
            # glTF/GLB stores coordinates in meters (spec). Convert to mm so all
            # metric computation (extents, volume, area) is consistent with STL/OBJ input.
            # cad_glb_bytes stays in meters — correct for BabylonJS rendering.
            mesh.apply_scale(1000.0)
            return mesh, glb_bytes

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
                mesh, cad_glb_bytes = _load_cad_with_cascadio(
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
                if len(mesh_data.geometry) > 1:
                    mesh = cast(trimesh.Trimesh, trimesh.util.concatenate(list(mesh_data.geometry.values())))
                else:
                    mesh = cast(trimesh.Trimesh, list(mesh_data.geometry.values())[0])
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
        if "MULTI_BODY_ERROR" in str(e):
            raise
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
    if isinstance(mesh_data, trimesh.Scene):
        if len(mesh_data.geometry) > 1:
            raise ValueError("MULTI_BODY_ERROR")
        if not mesh_data.geometry:
            raise ValueError("EMPTY_FILE_ERROR")
        mesh = cast(trimesh.Trimesh, list(mesh_data.geometry.values())[0])
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
                    if len(mesh_data.geometry) > 1:
                        raise ValueError("MULTI_BODY_ERROR")
                    if not mesh_data.geometry:
                        raise ValueError("EMPTY_FILE_ERROR")
                    mesh = cast(trimesh.Trimesh, list(mesh_data.geometry.values())[0])
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
    """Phase 2 worker: returns GLB bytes for the 3D viewer. Runs in a separate process."""
    if cad_glb_bytes:
        return cad_glb_bytes
    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            return None
        # STL is always in mm per convention. Convert back to meters for GLB
        # so BabylonJS renders the model at the correct physical size.
        mesh.apply_scale(0.001)
        return cast(bytes, mesh.export(file_type="glb"))
    except Exception as e:
        logger.warning(f"_export_glb_worker failed: {e}")
        return None


def _compute_dfm_worker(stl_bytes: bytes) -> dict[str, Any]:
    """Phase 2 worker: runs DFM analysis on the mesh. Runs in a separate process."""
    try:
        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", force="mesh")
        if not isinstance(mesh, trimesh.Trimesh):
            return {}

        extents = mesh.extents if mesh.extents is not None else [0.0, 0.0, 0.0]
        vol_bbox = float(extents[0]) * float(extents[1]) * float(extents[2])
        volume_mm3 = float(mesh.volume) if mesh.is_watertight else 0.0
        support_mm3 = max(0.0, vol_bbox - volume_mm3)

        thin_wall_count, thin_wall_centroids = _compute_thin_wall_analysis(mesh)
        overhang_face_count, overhang_area_cm2, overhang_centroids = (
            _compute_overhang_analysis(mesh)
        )
        sharp_corner_count, sharp_corner_centroids = _compute_sharp_corner_analysis(
            mesh
        )
        hollow_centroids = _compute_hollow_analysis(mesh)

        return {
            "FDM": {
                "reportType": "FDM",
                "thinWallCount": thin_wall_count,
                "thinWallRegions": thin_wall_centroids,
                "overhangFaceCount": overhang_face_count,
                "overhangAreaCm2": overhang_area_cm2,
                "overhangRegions": overhang_centroids,
                "supportRequired": overhang_face_count > 0 or thin_wall_count > 0,
                "estimatedSupportVolumeCm3": support_mm3 / 1000.0
                if overhang_face_count > 0
                else None,
                "smallDetailCount": 0,
            },
            "SLA": {
                "reportType": "SLA",
                "thinWallCount": thin_wall_count,
                "thinWallRegions": thin_wall_centroids,
                "overhangFaceCount": overhang_face_count,
                "overhangAreaCm2": overhang_area_cm2,
                "overhangRegions": overhang_centroids,
                "resinTrappingRisk": len(hollow_centroids) > 0,
                "resinTrappingRegions": hollow_centroids,
                "suctionRisk": overhang_face_count > 0 and len(mesh.faces) > 1000,
                "suctionRegions": overhang_centroids[:10]
                if overhang_face_count > 0
                else [],
                "hollowRegions": hollow_centroids,
            },
            "CNC": {
                "reportType": "CNC",
                "sharpCornerCount": sharp_corner_count,
                "sharpCornerRegions": sharp_corner_centroids,
                "hasUndercuts": False,
                "undercutRegions": [],
                "hasDrillHoles": False,
                "drillHoleCount": 0,
                "requiresEdm": sharp_corner_count > 20,
                "requiresGrinding": False,
                "minimumFeatureSizeMm": 1.0,
            },
        }
    except Exception as e:
        logger.warning(f"_compute_dfm_worker failed: {e}")
        return {}


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
                    if len(mesh_data.geometry) > 1:
                        raise ValueError("MULTI_BODY_ERROR")
                    if not mesh_data.geometry:
                        raise ValueError("EMPTY_FILE_ERROR")
                    mesh = cast(trimesh.Trimesh, list(mesh_data.geometry.values())[0])
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
