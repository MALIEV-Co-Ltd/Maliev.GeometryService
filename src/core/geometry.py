import os

import asyncio
import contextlib
import io
import logging
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, cast

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


def _generate_preview_images_sync(mesh: trimesh.Trimesh) -> dict[str, bytes | None]:
    """
    Generate 6-sided preview images using PyVista offscreen rendering.
    Returns a dictionary with keys: front, left, right, back, top, bottom.
    Uses VTK/OSMesa for headless CPU rendering (no GPU required).
    """
    results: dict[str, bytes | None] = {}
    empty: dict[str, bytes | None] = {"front": None, "back": None, "left": None, "right": None, "top": None, "bottom": None}

    try:
        import pyvista as pv

        pv.OFF_SCREEN = True
    except Exception as e:
        logger.error(f"Failed to import pyvista: {e}")
        return empty

    sides = {
        "front":  {"axis": (0, -1, 0), "viewup": (0, 0, 1)},
        "back":   {"axis": (0,  1, 0), "viewup": (0, 0, 1)},
        "left":   {"axis": (-1, 0, 0), "viewup": (0, 0, 1)},
        "right":  {"axis": (1,  0, 0), "viewup": (0, 0, 1)},
        "top":    {"axis": (0,  0, 1), "viewup": (0, 1, 0)},
        "bottom": {"axis": (0,  0, -1), "viewup": (0, -1, 0)},
    }

    try:
        faces_pv = np.column_stack([
            np.full(len(mesh.faces), 3, dtype=np.int32),
            mesh.faces
        ]).ravel()
        pv_mesh = pv.PolyData(mesh.vertices.copy(), faces_pv)
        pv_mesh.compute_normals(inplace=True)

        center = np.array(pv_mesh.center)
        bounds = np.array(pv_mesh.bounds)
        dims = np.array([bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]])
        max_dim = float(np.max(dims))
        distance = max_dim * 2.5

        for side_name, cam_config in sides.items():
            try:
                pl = pv.Plotter(off_screen=True, window_size=[512, 512])
                pl.set_background("white")

                pl.add_mesh(
                    pv_mesh,
                    color="#B0B0B0",
                    smooth_shading=True,
                    specular=0.5,
                    specular_power=30,
                    ambient=0.3,
                    diffuse=0.7,
                )

                axis = np.array(cam_config["axis"], dtype=float)
                viewup = np.array(cam_config["viewup"], dtype=float)
                camera_pos = center + axis * distance
                focal_point = center

                pl.camera_position = [
                    tuple(camera_pos),
                    tuple(focal_point),
                    tuple(viewup),
                ]

                pl.enable_parallel_projection()
                pl.reset_camera()

                pl.remove_all_lights()
                key_light = pv.Light(
                    position=(center[0] + max_dim * 3, center[1] - max_dim * 2, center[2] + max_dim * 3),
                    focal_point=tuple(center),
                    intensity=0.8,
                )
                pl.add_light(key_light)
                fill_light = pv.Light(
                    position=(center[0] - max_dim * 3, center[1] - max_dim, center[2] + max_dim),
                    focal_point=tuple(center),
                    intensity=0.4,
                )
                pl.add_light(fill_light)
                rim_light = pv.Light(
                    position=(center[0], center[1] + max_dim * 3, center[2] + max_dim * 2),
                    focal_point=tuple(center),
                    intensity=0.3,
                )
                pl.add_light(rim_light)

                pl.show(auto_close=False)
                img = pl.screenshot(transparent_background=False, return_img=True)
                pl.close()

                from PIL import Image

                pil_img = Image.fromarray(img)
                buf = io.BytesIO()
                pil_img.save(buf, format="PNG")
                buf.seek(0)
                results[side_name] = buf.getvalue()
                buf.close()

            except Exception as e:
                logger.error(f"Failed to generate preview image for side {side_name}: {e}")
                results[side_name] = None

    except Exception as e:
        logger.error(f"Failed to generate preview images: {e}")
        return empty

    for side in empty:
        if side not in results:
            results[side] = None

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
        with contextlib.suppress(Exception):
            preview_images = _generate_preview_images_sync(mesh)

        thumbnail_bytes = preview_images.get("front")

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
        try:
            preview_images = _generate_preview_images_sync(mesh)
            return preview_images.get("front")
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

        return metrics, result["glb_bytes"], result["thumbnail_bytes"], result.get("preview_images", {})
