# Data Model: DFM Analysis Integration

**Date**: 2026-02-21
**Feature**: 001-dfm-analysis

## Entity Definitions

### DfmReport (NEW)

Pydantic model containing DFM analysis results.

| Field                  | Type                 | Description                                    | Constraints        |
| ---------------------- | -------------------- | ---------------------------------------------- | ------------------ |
| `thin_wall_count`      | `int`                | Number of thin wall sample points detected     | >= 0               |
| `thin_wall_regions`    | `list[list[float]]`  | Centroid coordinates of thin wall regions [mm] | Each inner list length 3 |
| `overhang_face_count`  | `int`                | Number of faces classified as overhangs        | >= 0               |
| `overhang_area_cm2`    | `float`              | Total area of overhang faces in cm²            | >= 0.0             |

**JSON Serialization (CamelCase aliases)**:
```json
{
  "thinWallCount": 15,
  "thinWallRegions": [[25.3, 12.1, 5.0], [30.2, 15.8, 5.0]],
  "overhangFaceCount": 120,
  "overhangAreaCm2": 3.45
}
```

### GeometryMetrics (EXTENDED)

Existing model extended with optional DFM report.

| Field                  | Type                 | Description                                    | Constraints        |
| ---------------------- | -------------------- | ---------------------------------------------- | ------------------ |
| `volume_cm3`           | `float`              | Part volume in cm³                             | >= 0               |
| `support_volume_cm3`   | `float`              | Support volume estimate in cm³                 | >= 0               |
| `surface_area_cm2`     | `float`              | Surface area in cm²                            | >= 0               |
| `bounding_box`         | `BoundingBox`        | Bounding box dimensions in mm                  | Required           |
| `is_manifold`          | `bool`               | Whether mesh is watertight                     | Required           |
| `triangle_count`       | `int`                | Number of triangles in mesh                    | >= 0               |
| `euler_number`         | `int`                | Euler characteristic of mesh                   | Required           |
| `dfm_report`           | `DfmReport \| None`  | DFM analysis results (NEW)                     | Optional, default None |

### BoundingBox (UNCHANGED)

| Field   | Type    | Description       |
| ------- | ------- | ----------------- |
| `x`     | `float` | X dimension (mm)  |
| `y`     | `float` | Y dimension (mm)  |
| `z`     | `float` | Z dimension (mm)  |

## State Transitions

Not applicable - DFM analysis is stateless per-file analysis.

## Validation Rules

1. **DfmReport validation**:
   - All counts must be non-negative integers
   - `thin_wall_regions` must contain valid 3D coordinates
   - `overhang_area_cm2` must be non-negative float

2. **Graceful degradation**:
   - If DFM analysis fails, `dfm_report` is `None` (not zero-filled)
   - If mesh is degenerate, return zero-filled `DfmReport` (not `None`)

3. **Unit consistency**:
   - Coordinates in millimeters (mm)
   - Area in square centimeters (cm²)
   - Matches existing `GeometryMetrics` conventions

## Relationships

```
FileAnalyzedMessage
    └── metrics: GeometryMetrics
            ├── bounding_box: BoundingBox
            └── dfm_report: DfmReport (optional)
                    └── thin_wall_regions: list[[x, y, z]]
```
