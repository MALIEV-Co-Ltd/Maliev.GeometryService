import numpy as np
import numpy.typing as npt
import trimesh
from pydantic import BaseModel, ConfigDict, Field


def to_camel(string: str) -> str:
    components = string.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


class DfmReport(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    thin_wall_count: int = Field(alias="thinWallCount", ge=0)
    thin_wall_regions: list[list[float]] = Field(
        alias="thinWallRegions", default_factory=list
    )
    overhang_face_count: int = Field(alias="overhangFaceCount", ge=0)
    overhang_area_cm2: float = Field(alias="overhangAreaCm2", ge=0.0)


class DfmAnalyzer:
    def __init__(
        self,
        sample_count: int = 500,
        min_thickness_mm: float = 0.8,
        critical_angle_deg: float = 45.0,
    ) -> None:
        self.sample_count = sample_count
        self.min_thickness_mm = min_thickness_mm
        self.critical_angle_deg = critical_angle_deg

    def detect_thin_walls(self, mesh: trimesh.Trimesh) -> tuple[int, list[list[float]]]:
        if len(mesh.faces) == 0:
            return 0, []
        points, face_indices = trimesh.sample.sample_surface(mesh, self.sample_count)
        if len(points) == 0:
            return 0, []
        ray_origins = np.array(points, dtype=np.float64)
        ray_directions = -mesh.face_normals[face_indices]
        locations, index_ray, _index_tri = mesh.ray.intersects_location(  # type: ignore[no-untyped-call]
            ray_origins=ray_origins, ray_directions=ray_directions
        )
        if len(locations) == 0:
            return 0, []
        thin_regions: list[list[float]] = []
        ray_hit_map: dict[int, list[tuple[float, npt.NDArray[np.float64]]]] = {}
        for hit_idx in range(len(locations)):
            ray_idx = int(index_ray[hit_idx])
            origin = ray_origins[ray_idx]
            hit_point = locations[hit_idx]
            distance = float(np.linalg.norm(hit_point - origin))
            if distance > 1e-6:
                if ray_idx not in ray_hit_map:
                    ray_hit_map[ray_idx] = []
                ray_hit_map[ray_idx].append((distance, origin))
        for _ray_idx, hits in ray_hit_map.items():
            min_distance = min(h[0] for h in hits)
            if min_distance < self.min_thickness_mm:
                origin = hits[0][1]
                thin_regions.append(
                    [float(origin[0]), float(origin[1]), float(origin[2])]
                )
        return len(thin_regions), thin_regions

    def detect_overhangs(self, mesh: trimesh.Trimesh) -> tuple[int, float]:
        if len(mesh.faces) == 0:
            return 0, 0.0
        normals = mesh.face_normals
        downward = np.array([0.0, 0.0, -1.0])
        dot_products = np.dot(normals, downward)
        threshold = float(np.cos(np.radians(90.0 - self.critical_angle_deg)))
        overhang_mask = dot_products > threshold
        overhang_count = int(np.sum(overhang_mask))
        overhang_area_mm2 = float(np.sum(mesh.area_faces[overhang_mask]))
        overhang_area_cm2 = overhang_area_mm2 / 100.0
        return overhang_count, overhang_area_cm2

    def analyze(self, mesh: trimesh.Trimesh) -> DfmReport:
        try:
            thin_count, thin_regions = self.detect_thin_walls(mesh)
            overhang_count, overhang_area = self.detect_overhangs(mesh)
            return DfmReport(
                thinWallCount=thin_count,
                thinWallRegions=thin_regions,
                overhangFaceCount=overhang_count,
                overhangAreaCm2=overhang_area,
            )
        except Exception:
            return DfmReport(
                thinWallCount=0,
                thinWallRegions=[],
                overhangFaceCount=0,
                overhangAreaCm2=0.0,
            )
