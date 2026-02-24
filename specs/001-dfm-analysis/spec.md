# Feature Specification: DFM Analysis Integration

**Feature Branch**: `001-dfm-analysis`
**Created**: 2026-02-21
**Status**: Draft
**Input**: User description: "DFM Analysis Integration (Maliev.GeometryService) - Extends geometry analysis with Design for Manufacturability checks for thin walls and overhangs"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Thin Wall Detection (Priority: P1)

As a manufacturing engineer, when I upload a 3D model file, the system automatically detects regions where wall thickness falls below the minimum threshold (0.8mm by default) and reports the count and locations of these thin wall regions.

**Why this priority**: Thin walls are a critical print failure mode for FDM 3D printing. Detecting them before quoting prevents production issues and customer complaints.

**Independent Test**: Can be fully tested by uploading a thin plate model (e.g., 50x50x0.5mm) and verifying the response includes `dfmReport.thinWallCount > 0` with coordinate locations.

**Acceptance Scenarios**:

1. **Given** a 3D file with walls thinner than 0.8mm, **When** the file is analyzed, **Then** the response includes a DFM report with `thinWallCount > 0` and at least one coordinate location
2. **Given** a solid 10x10x10mm cube, **When** the file is analyzed, **Then** the response includes a DFM report with `thinWallCount = 0`
3. **Given** a mesh where ray-casting fails for some sample points, **When** the file is analyzed, **Then** the analysis continues without error, skipping failed points

---

### User Story 2 - Overhang Detection (Priority: P1)

As a manufacturing engineer, when I upload a 3D model file, the system automatically detects faces that would require support material due to overhang angles exceeding the critical threshold (45 degrees by default).

**Why this priority**: Overhang detection directly impacts support structure requirements and pricing accuracy for FDM printing.

**Independent Test**: Can be fully tested by uploading a model with downward-facing faces and verifying the response includes `dfmReport.overhangFaceCount > 0` and `overhangAreaCm2` values.

**Acceptance Scenarios**:

1. **Given** a model with faces angled more than 45 degrees from horizontal (pointing downward), **When** the file is analyzed, **Then** the response includes a DFM report with `overhangFaceCount > 0` and a positive `overhangAreaCm2`
2. **Given** a model with all faces pointing upward or horizontal, **When** the file is analyzed, **Then** the response includes a DFM report with `overhangFaceCount = 0`

---

### User Story 3 - Graceful Degradation on Edge Cases (Priority: P2)

As a system operator, when a malformed or degenerate mesh is uploaded, the geometry analysis completes successfully while the DFM report returns zeros instead of failing the entire analysis.

**Why this priority**: Ensures service reliability - DFM analysis is additive and should not block the primary geometry metrics flow.

**Independent Test**: Can be fully tested by uploading an empty or degenerate mesh and verifying the main analysis completes with a zero-filled DFM report.

**Acceptance Scenarios**:

1. **Given** an empty or degenerate mesh, **When** analysis is performed, **Then** the system returns a DFM report with all zeros: `thinWallCount=0`, `thinWallRegions=[]`, `overhangFaceCount=0`, `overhangAreaCm2=0.0`
2. **Given** a valid mesh where DFM analysis throws an exception, **When** analysis is performed, **Then** the geometry metrics are still returned successfully with `dfmReport=null`

---

### Edge Cases

- What happens when ray-casting fails for individual sample points? (Skip silently, continue analysis)
- How does the system handle a mesh with no faces? (Return zero-filled DFM report)
- What happens when the mesh has inverted normals? (Analysis proceeds; overhang detection may produce incorrect results since normals point outward from interior - this is acceptable as fixing inverted normals is outside DFM scope)
- How does the system handle very large meshes? (Must complete within 5 seconds per SC-003; sampling is limited to 500 points to ensure performance)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST detect thin wall regions by sampling 500 surface points and measuring wall thickness via ray-casting
- **FR-002**: System MUST identify points where wall thickness is less than the minimum threshold (default 0.8mm) as thin wall candidates
- **FR-003**: System MUST return the count of thin wall points and their coordinates (sample point locations in mm where thin wall was detected)
- **FR-004**: System MUST detect overhang faces by computing the angle between each face normal and the downward Z-axis [0, 0, -1]
- **FR-005**: System MUST classify faces with normal angles less than 45 degrees from the downward Z-axis as overhangs
- **FR-006**: System MUST return the count of overhang faces and total overhang area in square centimeters
- **FR-007**: System MUST include the DFM report in the `FileAnalyzedEvent` message payload
- **FR-008**: System MUST NOT fail the overall geometry analysis if DFM analysis encounters an error
- **FR-009**: System MUST silently skip sample points where ray-casting fails during thin wall detection

### Key Entities

- **DfmReport**: A data structure containing `thinWallCount` (integer), `thinWallRegions` (list of [x,y,z] coordinates in mm), `overhangFaceCount` (integer), and `overhangAreaCm2` (float)
- **GeometryMetrics**: Extended to optionally include a `dfmReport` field containing DFM analysis results

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Thin wall detection identifies walls thinner than 0.8mm with greater than 90% accuracy on test meshes
- **SC-002**: Overhang detection correctly classifies faces angled more than 45 degrees from horizontal
- **SC-003**: DFM analysis completes within 5 seconds for typical models (under 100,000 triangles)
- **SC-004**: DFM analysis failures do not affect the success rate of geometry analysis (0% impact on existing metrics)
- **SC-005**: All existing geometry tests continue to pass unchanged

## Assumptions

- Input mesh units are in millimeters (mm) as per existing system convention
- Default minimum wall thickness of 0.8mm is appropriate for FDM printing (may be parameterized in future)
- Default critical overhang angle of 45 degrees is appropriate for FDM printing
- Sampling 500 surface points provides sufficient coverage for thin wall detection
- The existing `trimesh` library provides adequate ray-casting capabilities
