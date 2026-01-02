import asyncio
import io
from concurrent.futures import ProcessPoolExecutor

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

            mesh_data = trimesh.load(
                file_stream, file_type=file_extension.strip("."), force="mesh"
            )

            if isinstance(mesh_data, trimesh.Scene):
                # Requirement FR-003.1: Reject files containing multiple disjoint bodies
                if len(mesh_data.geometry) > 1:
                    raise ValueError("MULTI_BODY_ERROR")
                # Get the first (and only) geometry
                mesh = list(mesh_data.geometry.values())[0]
            else:
                mesh = mesh_data

            if not isinstance(mesh, trimesh.Trimesh):
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
            bbox = BoundingBox(
                x=float(extents[0]), y=float(extents[1]), z=float(extents[2])
            )

            # Support Volume Estimation (Bounding box approximation Z-up)
            vol_bbox = float(extents[0]) * float(extents[1]) * float(extents[2])
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
