import asyncio
import contextlib
import io
import os
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, cast

import gmsh
import numpy as np
import trimesh
from pydantic import BaseModel, ConfigDict, Field


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
        thumbnail_bytes: bytes | None = None

        with contextlib.suppress(Exception):
            glb_bytes = cast(bytes, mesh.export(file_type="glb"))

        with contextlib.suppress(Exception):
            scene = trimesh.Scene(geometry=mesh)
            png_bytes = scene.save_image(resolution=(512, 512), visible=True)
            if isinstance(png_bytes, bytes):
                thumbnail_bytes = png_bytes

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
            scene = trimesh.Scene(geometry=mesh)
            png_bytes = scene.save_image(resolution=(512, 512), visible=True)
            if isinstance(png_bytes, bytes):
                return png_bytes
            return None
        except Exception:
            return None

    async def analyze_async(
        self, file_stream: io.BytesIO, file_extension: str
    ) -> tuple[GeometryMetrics, bytes | None, bytes | None]:
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

        return metrics, result["glb_bytes"], result["thumbnail_bytes"]
