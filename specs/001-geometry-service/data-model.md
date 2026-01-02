# Data Model: Geometry Analysis Service

## Core Entities

### AnalysisRequest
Represents the input payload for a processing job.

| Field | Type | Description |
|-------|------|-------------|
| `file_id` | `UUID` | Unique identifier of the file. |
| `storage_bucket` | `str` | S3/MinIO bucket name. |
| `storage_key` | `str` | Path to the file within the bucket. |
| `content_type` | `str` | MIME type (e.g., `model/stl`, `application/step`). |

### GeometryMetrics
The result of the geometric analysis.

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `volume_cm3` | `float` | cm³ | Volume of the closed mesh. |
| `support_volume_cm3` | `float` | cm³ | Estimated support volume (Z-up bounding box projection). |
| `surface_area_cm2` | `float` | cm² | Total surface area of the mesh. |
| `bounding_box` | `BoundingBox` | mm | Axis-aligned bounding box. |
| `is_manifold` | `bool` | - | True if mesh is watertight and has no non-manifold edges. |
| `triangle_count` | `int` | - | Number of faces in the mesh. |
| `euler_number` | `int` | - | Topological characteristic (V - E + F). |

### BoundingBox
Dimensions of the object.

| Field | Type | Unit |
|-------|------|------|
| `x` | `float` | mm |
| `y` | `float` | mm |
| `z` | `float` | mm |

## Validation Rules

- **Units**: All input coordinates for STL/OBJ are assumed to be **Millimeters (mm)**.
- **Volume**: Must be >= 0.
- **Files**:
  - Max Size: **200 MB**.
  - Multi-body STEP files: **Rejected**.
  - Non-manifold files: **Accepted** (Success event with `is_manifold: false`, uses **Convex Hull** for metrics).

## Event Payloads

### FileAnalyzedEvent
Published on success.
- `fileId`: UUID
- `metrics`: GeometryMetrics
- `processedAt`: DateTime (ISO8601)

### FileAnalysisFailedEvent
Published on failure.
- `fileId`: UUID
- `error`: ErrorCode (`GEOMETRY_NON_MANIFOLD`, `FILE_CORRUPT`, `SIZE_LIMIT_EXCEEDED`, `TIMEOUT`, `MULTI_BODY_ERROR`)
- `details`: String (optional description)
