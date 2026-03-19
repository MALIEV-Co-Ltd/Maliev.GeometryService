import asyncio
import contextlib
import io
import logging
import os
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, TypedDict, cast

import gmsh
import numpy as np
import trimesh
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


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

    pl.show(auto_close=False)
    img = pl.screenshot(transparent_background=False, return_img=True)
    pl.close()

    pil_img = Image.fromarray(img)
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def _generate_preview_images_sync(mesh: trimesh.Trimesh) -> dict[str, bytes | None]:
    """
    Generate 7 preview images (6 orthographic + 1 isometric) at 256x256.
    Uses VTK/OSMesa for headless CPU rendering (no GPU required).
    """
    results: dict[str, bytes | None] = {}
    empty: dict[str, bytes | None] = {f"{view}_256": None for view in ALL_VIEWS}

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
                )
                results[f"{view_name}_256"] = image_bytes
            except Exception as e:
                logger.error(
                    f"Failed to generate preview image for view {view_name}: {e}"
                )
                results[f"{view_name}_256"] = None

    except Exception as e:
        logger.error(f"Failed to generate preview images: {e}")
        return empty

    for key in empty:
        if key not in results:
            results[key] = None

    return results


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

                try:
                    gmsh.initialize()
                    gmsh.option.setNumber("General.Verbosity", 0)
                    gmsh.open(tmp_path)
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

        thumbnail_bytes = preview_images.get("iso_256")

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


class GeometryProcessor:
    def __init__(self) -> None:
        self.executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 4)

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
            return preview_images.get("iso_256")
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
