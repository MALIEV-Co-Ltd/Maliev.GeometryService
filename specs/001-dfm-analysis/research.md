# Research: DFM Analysis Integration

**Date**: 2026-02-21
**Feature**: 001-dfm-analysis

## Decision 1: Thin Wall Detection via Ray-Casting

**Decision**: Use `trimesh.proximity.closest_point()` combined with ray intersection for thickness measurement.

**Rationale**:
- `trimesh.sample.sample_surface(mesh, count)` returns sample points and face indices
- For each sample point, get the face normal, negate it for inward direction
- Use `mesh.ray.intersects_location()` to cast rays and find intersection points
- Distance from sample to intersection = local thickness

**Alternatives Considered**:
- `trimesh.proximity.thicknes()` - Not available in trimesh standard API
- Voxel-based thickness estimation - More accurate but computationally expensive
- Custom sphere-packing approach - Overkill for MVP

**Implementation Pattern**:
```python
# Sample surface points
points, face_indices = trimesh.sample.sample_surface(mesh, 500)

# For each point, get inward ray direction (negative face normal)
face_normal = mesh.face_normals[face_idx]
ray_direction = -face_normal

# Cast ray and measure distance
locations, index_ray, index_tri = mesh.ray.intersects_location(
    ray_origins=[point],
    ray_directions=[ray_direction]
)
```

## Decision 2: Overhang Detection via Normal Angle

**Decision**: Compute dot product between face normals and downward Z-axis `[0, 0, -1]`.

**Rationale**:
- `mesh.face_normals` provides all face normals as numpy array
- Dot product with `[0, 0, -1]` gives cos(angle) between normal and downward direction
- A face is an overhang if `dot_product > cos(90 - critical_angle)`
- For 45° critical angle: `cos(45°) ≈ 0.707`
- So: `dot_product > 0.707` means face points more than 45° downward

**Alternatives Considered**:
- Per-vertex normal analysis - More granular but face-level sufficient for area calculation
- Separate normal computation - Unnecessary, trimesh provides normals

**Implementation Pattern**:
```python
import numpy as np

# Get all face normals
normals = mesh.face_normals

# Downward Z-axis
downward = np.array([0, 0, -1])

# Dot product for all faces at once
dot_products = np.dot(normals, downward)

# Critical angle threshold
critical_angle = 45.0  # degrees
threshold = np.cos(np.radians(90 - critical_angle))  # cos(45°) ≈ 0.707

# Overhang mask
overhang_mask = dot_products > threshold

# Count and area
overhang_count = np.sum(overhang_mask)
overhang_area_mm2 = np.sum(mesh.area_faces[overhang_mask])
overhang_area_cm2 = overhang_area_mm2 / 100.0
```

## Decision 3: Error Handling Strategy

**Decision**: Wrap all DFM operations in try/except, return zero-filled report on failure.

**Rationale**:
- DFM is additive value, not critical path
- Per spec FR-008: "System MUST NOT fail the overall geometry analysis if DFM analysis encounters an error"
- Degenerate meshes may lack normals or have insufficient faces

**Implementation Pattern**:
```python
def analyze(self, mesh: trimesh.Trimesh) -> DfmReport:
    try:
        thin_count, thin_regions = self.detect_thin_walls(mesh)
        overhang_count, overhang_area = self.detect_overhangs(mesh)
        return DfmReport(
            thin_wall_count=thin_count,
            thin_wall_regions=thin_regions,
            overhang_face_count=overhang_count,
            overhang_area_cm2=overhang_area
        )
    except Exception:
        return DfmReport(
            thin_wall_count=0,
            thin_wall_regions=[],
            overhang_face_count=0,
            overhang_area_cm2=0.0
        )
```

## Decision 4: Sample Count for Thin Wall Detection

**Decision**: Use 500 sample points as specified.

**Rationale**:
- Spec FR-001 mandates 500 sample points
- Provides good coverage for typical part sizes
- Performance acceptable: 500 ray casts is trivial for trimesh

**Alternatives Considered**:
- Adaptive sampling based on mesh complexity - Over-engineering for MVP
- Higher count (1000+) - Diminishing returns, longer processing time

## Decision 5: Message Schema Integration

**Decision**: Embed `DfmReport` within `GeometryMetrics`, which flows through `FileAnalyzedMessage.metrics`.

**Rationale**:
- `GeometryMetrics` is already part of `FileAnalyzedMessage`
- Adding `dfm_report: DfmReport | None` to `GeometryMetrics` keeps schema clean
- Nested structure: `message.metrics.dfmReport` in JSON
- No breaking change to existing consumers

**Schema Change**:
```python
class GeometryMetrics(BaseModel):
    volume_cm3: float
    support_volume_cm3: float
    surface_area_cm2: float
    bounding_box: BoundingBox
    is_manifold: bool
    triangle_count: int
    euler_number: int
    dfm_report: DfmReport | None = None  # NEW
```
