import asyncio
import io
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import cast

import gmsh
import numpy as np
import trimesh
from pydantic import BaseModel


class BoundingBox(BaseModel):
    x: float
    y: float
    z: float


class GeometryMetrics(BaseModel):
    volume_cm3: float
    support_volume_cm3: float
    surface_area_cm2: float
    bounding_box: BoundingBox
    is_manifold: bool
    triangle_count: int
    euler_number: int


class GeometryProcessor:
    def __init__(self) -> None:
        # We'll use this for offloading CPU bound tasks if needed
        self.executor = ProcessPoolExecutor(max_workers=4)

    def analyze_stream(
        self, file_stream: io.BytesIO, file_extension: str
    ) -> GeometryMetrics:
        """
        Analyzes a 3D file stream and extracts geometric metrics.
        Assumes input units are Millimeters (mm).
        """
        try:
            # Reset stream position just in case
            file_stream.seek(0)
            ext = file_extension.strip(".").lower()

            mesh = None
            if ext in ["igs", "iges", "step", "stp"]:
                # Use explicit GMSH tessellation for CAD formats
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=f".{ext}", delete=False
                    ) as tmp:
                        tmp.write(file_stream.getvalue())
                        tmp_path = tmp.name

                    try:
                        gmsh.initialize()
                        gmsh.option.setNumber("General.Verbosity", 0)
                        gmsh.open(tmp_path)
                        gmsh.model.mesh.generate(2)

                        _, coords, _ = gmsh.model.mesh.getNodes()
                        v = coords.reshape((-1, 3))
                        _, _, node_tags = gmsh.model.mesh.getElements(2)

                        if len(node_tags) > 0:
                            f = np.array(node_tags[0]) - 1
                            mesh = trimesh.Trimesh(vertices=v, faces=f.reshape((-1, 3)))
                        else:
                            raise ValueError("GMSH_TESSELLATION_FAILED")
                    finally:
                        gmsh.finalize()
                        p = Path(tmp_path)
                        if p.exists():
                            p.unlink()
                except Exception as e:
                    raise ValueError(f"CAD_LOAD_ERROR: {ext} ({str(e)})") from e
            else:
                # Standard mesh formats (STL, OBJ, 3MF)
                # Try to load using trimesh natively
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

            # Calculate metrics (mm -> cm)
            if is_manifold:
                volume_mm3 = float(mesh.volume)
                area_mm2 = float(mesh.area)
            else:
                # Fallback to Convex Hull for non-manifold meshes
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
                # Fallback if extents are not available (e.g. empty or corrupt mesh)
                bbox = BoundingBox(x=0.0, y=0.0, z=0.0)
                vol_bbox = 0.0
            else:
                bbox = BoundingBox(
                    x=float(extents[0]), y=float(extents[1]), z=float(extents[2])
                )
                vol_bbox = float(extents[0]) * float(extents[1]) * float(extents[2])

            # Support Volume Estimation (Bounding box approximation Z-up)
            support_mm3 = max(0.0, vol_bbox - volume_mm3)

            return GeometryMetrics(
                volume_cm3=volume_mm3 / 1000.0,
                support_volume_cm3=support_mm3 / 1000.0,
                surface_area_cm2=area_mm2 / 100.0,
                bounding_box=bbox,
                is_manifold=is_manifold,
                triangle_count=len(mesh.faces),
                euler_number=euler_number,
            )

        except Exception as e:
            if "MULTI_BODY_ERROR" in str(e):
                raise e from e
            raise ValueError(f"FILE_CORRUPT: {str(e)}") from e

    async def analyze_async(
        self, file_stream: io.BytesIO, file_extension: str
    ) -> GeometryMetrics:
        """Wrapper to run analyze_stream in a separate thread to avoid blocking."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self.analyze_stream, file_stream, file_extension
        )
