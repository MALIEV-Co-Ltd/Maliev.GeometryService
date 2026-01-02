# Feature Specification: Geometry Analysis Service

**Feature Branch**: `001-geometry-service`
**Created**: 2025-01-02
**Status**: Draft
**Input**: User description: "Geometry Analysis Service"

## Clarifications

### Session 2025-01-02
- Q: How to handle STEP files with multiple solid bodies? → A: Reject with error (System only supports single-body files).
- Q: What unit to assume for unitless files (STL/OBJ)? → A: Millimeters (mm).
- Q: Should the service calculate support material volume? → A: Yes, provided as a bounding-box based estimation.
- Q: What is the maximum file size limit? → A: 200 MB.
- Q: What is the expected retry policy for transient download failures? → A: 3 attempts with exponential backoff.
- Q: Should a non-manifold file result in a Success or Failure event? → A: Report as Success (with `IsWatertight: false` and best-effort metrics).
- Q: How should the service determine orientation for support volume calculation? → A: Use file's default orientation (Z-up is build direction).
- Q: Is explicit file cleanup required? → A: Yes, mandatory deletion after processing to prevent disk exhaustion.

## User Scenarios & Testing

### User Story 1 - Automated Geometry Analysis (Priority: P1)

As the System (Upload Workflow), I want to offload geometric calculations to a dedicated service so that the main upload API remains responsive and scalable.

**Why this priority**: Core functionality required to enable accurate pricing and validation without blocking the user interface.

**Independent Test**: Can be tested by publishing a `FileUploadedEvent` to the queue and verifying that the corresponding `FileAnalyzedEvent` is published with correct metrics or `FileAnalysisFailedEvent` is published for invalid files.

**Acceptance Scenarios**:

1. **Given** a valid 3D file (STL/OBJ) is uploaded and event published, **When** the service processes the message, **Then** a `FileAnalyzedEvent` is published containing Volume, Surface Area, Bounding Box, and Manifold status.
2. **Given** a corrupt or invalid 3D file, **When** the service processes the message, **Then** a `FileAnalysisFailedEvent` is published with an appropriate error code.
3. **Given** a massive file that exceeds processing limits, **When** the service processes the message, **Then** it fails gracefully and reports the failure.
4. **Given** a non-manifold file, **When** analyzed, **Then** the service reports `isManifold: false` and uses the **Convex Hull** to calculate best-effort Volume and Area.

---

### User Story 2 - Accurate Quotation Data (Priority: P1)

As the Quotation Service, I need accurate geometric metrics (Volume, Surface Area, Bounding Box) to calculate raw material costs and machine fit.

**Why this priority**: Essential for the business model; we cannot price parts without this data.

**Independent Test**: Verify that the calculated metrics for known standard shapes (e.g., a 10x10x10cm cube) match the expected theoretical values within a small tolerance.

**Acceptance Scenarios**:

1. **Given** a 10cm cube file, **When** analyzed, **Then** the volume is reported as 1000 cm³ and surface area as 600 cm².
2. **Given** a part with support material requirements, **When** analyzed, **Then** the volume accurately reflects the raw material needed.
3. **Given** a part larger than the printer's build volume, **When** analyzed, **Then** the Bounding Box dimensions allow the Quotation Service to flag it as unprintable.

---

### User Story 3 - Printability Validation (Priority: P2)

As a Customer, I want to know immediately if my file is "Non-Manifold" (has holes or geometry errors) so I can fix it before ordering.

**Why this priority**: Improves customer experience by preventing failed orders and reducing manual review time.

**Independent Test**: Upload a known non-manifold file (e.g., a cube with a missing face) and verify the analysis result flags it as `IsWatertight: false`.

**Acceptance Scenarios**:

1. **Given** a non-manifold 3D file, **When** processed, **Then** the result indicates `IsWatertight` is false.
2. **Given** a perfectly watertight file, **When** processed, **Then** the result indicates `IsWatertight` is true.

---

### User Story 4 - Native CAD Support (Priority: P2)

As a CAD Engineer, I want to upload native STEP (.stp/.step) files directly without manual conversion to STL.

**Why this priority**: Streamlines the workflow for professional engineers and preserves geometry fidelity.

**Independent Test**: Upload a valid STEP file and verify it is processed successfully with correct metrics.

**Acceptance Scenarios**:

1. **Given** a standard STEP file, **When** uploaded, **Then** it is processed successfully and metrics are generated.
2. **Given** a STEP file with multiple bodies, **When** uploaded, **Then** the service fails the analysis with a `MULTI_BODY_ERROR` code.

### Edge Cases

- **Zero Volume / Degenerate Mesh**: File is valid format but contains no volume (e.g., a single triangle or flat plane). System should return 0 volume and flag as potentially printable only if intended (likely User Warning).
- **Corrupt File Header**: File upload incomplete or header invalid. System fails immediately and triggers `FileAnalysisFailedEvent` with `FILE_CORRUPT`.
- **Format Mismatch**: File extension says `.stl` but content is text/binary garbage. System detects mismatch and fails.
- **Extremely Large Coordinates**: Mesh coordinates exceed float precision or physical build volume (e.g., modeled in meters instead of mm). System validates scale.
- **Password Protected Archives**: If ZIP support is added later; currently treated as invalid format.
- **Multi-body Files**: File contains multiple disjoint solids. System detects count > 1 and fails analysis.
- **File Exceeds Size Limit**: File is larger than 200 MB. System rejects before or during processing.

## Requirements

### Functional Requirements

- **FR-001**: System MUST subscribe to file upload events from the messaging system.
- **FR-002**: System MUST download the specified 3D file from blob storage using the provided location.
- **FR-002.1**: System MUST delete the local temporary file immediately after analysis completion (success or failure) to prevent disk exhaustion.
- **FR-003**: System MUST support processing of `.stl`, `.obj`, `.step`, and `.stp` file formats.
- **FR-003.1**: System MUST reject files containing multiple disjoint bodies (e.g., assemblies) with a specific error.
- **FR-003.2**: System MUST assume Millimeters (mm) as the base unit for all unitless file formats (STL, OBJ).
- **FR-003.3**: System MUST reject files larger than 200 MB with a specific error code.
- **FR-004**: System MUST calculate the Mesh Volume in cm³.
- **FR-004.1**: System MUST provide an estimated Support Material Volume (cm³) based on bounding box approximation using the file's original orientation (assuming Z-up is build direction).
- **FR-005**: System MUST calculate the Surface Area in cm².
- **FR-006**: System MUST calculate the Axis-Aligned Bounding Box (AABB) dimensions (X, Y, Z) in mm.
- **FR-007**: System MUST determine if the mesh is Manifold.
- **FR-008**: System MUST calculate the Euler Number for topological analysis.
- **FR-009**: System MUST publish a success event with all calculated metrics upon successful analysis.
- **FR-010**: System MUST publish a failure event with specific error codes (e.g., `FILE_CORRUPT`, `TIMEOUT`, `SIZE_LIMIT_EXCEEDED`) if analysis fails. Note: Non-manifold geometry results in a success event with `isManifold: false` and fallback to Convex Hull metrics.
- **FR-011**: System MUST instrument all processing steps with distributed tracing and metrics.
- **FR-013**: System MUST provide Scalar OpenAPI documentation at `/geometry/scalar`.
- **FR-014**: System MUST be accessible via the `/geometry` route prefix to support Kubernetes Ingress routing.
- **FR-015**: System MUST provide health check endpoints at `/geometry/liveness` and `/geometry/readiness`.
- **FR-0012**: System MUST retry failed processing for transient errors (e.g., network download failure) up to 3 times with exponential backoff.

### Key Entities

- **AnalysisRequest**: Represents a job to process a specific file, containing file ID, storage location, and metadata.
- **GeometryMetrics**: The core data output containing Volume, SupportVolume, Area, BoundingBox (SizeX, SizeY, SizeZ), isManifold, and EulerNumber.
- **AnalysisResult**: The final output object wrapping GeometryMetrics or Error details, linked to the original File ID.

## Success Criteria

### Measurable Outcomes

- **SC-001**: Analysis for a standard 50MB STL file completes in under 5 seconds (excluding download time).
- **SC-002**: System successfully processes 99.9% of valid standard STL files.
- **SC-003**: System correctly identifies non-manifold geometry with >99% accuracy against a test suite of known bad files.
- **SC-004**: System scales horizontally to handle bursts of concurrent uploads without dropping messages or exceeding 30s max latency for any single file.
