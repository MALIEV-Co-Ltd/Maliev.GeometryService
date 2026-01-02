# Tasks: Geometry Analysis Service

## Phase 1: Setup & Skeleton [P]

- [x] **T1-001**: Initialize Python project with Poetry and configure `pyproject.toml` (Strict typing, ruff, mypy).
- [x] **T1-002**: Create Dockerfile with multi-stage build including `gmsh` and `meshio` dependencies.
- [x] **T1-003**: Implement FastAPI application with `/geometry` root path prefix.
- [x] **T1-004**: Integrate `scalar-fastapi` and expose documentation at `/geometry/scalar`.
- [x] **T1-005**: Setup health check endpoints (`/geometry/liveness`, `/geometry/readiness`).
- [x] **T1-006**: Configure logging and distributed tracing (OpenTelemetry) boilerplate.

## Phase 2: The Math Core [P]

- [x] **T2-001**: **[Test-First]** Create unit tests with `cube.stl` and `broken.stl` (manifold check) and define test suites for metrics.
- [x] **T2-002**: Implement `GeometryProcessor` in `src/core/geometry.py` using `trimesh` to pass manifold and volume tests.
- [x] **T2-003**: Add support for STL/OBJ unit assumptions (default to mm).
- [x] **T2-004**: Implement bounding box based support volume estimation and fallback Convex Hull for non-manifold meshes.
- [x] **T2-005**: Implement STEP file tessellation handling using `gmsh`.
- [x] **T2-006**: Ensure `analyze_stream` runs in a separate thread/process to avoid blocking.

## Phase 3: Messaging & Storage [P]

- [x] **T3-001**: **[Test-First]** Define contract tests for RabbitMQ messages and Storage service mocks.
- [x] **T3-002**: Implement `StorageService` in `src/infrastructure/storage.py` (MinIO/S3 support).
- [x] **T3-003**: Implement 3-attempt retry logic with exponential backoff for file downloads.
- [x] **T3-004**: Implement mandatory file cleanup logic after processing.
- [x] **T3-005**: Implement `aio-pika` consumer for `FileUploadedEvent` in `src/consumers/upload_consumer.py`.
- [x] **T3-006**: Implement event publisher for `FileAnalyzedEvent` (including `eulerNumber`) and `FileAnalysisFailedEvent`.
- [x] **T3-007**: Handle multi-body STEP files by rejecting with `MULTI_BODY_ERROR`.
- [x] **T3-008**: Enforce 200MB file size limit before processing.

## Phase 4: Finalization & Integration [P]

- [x] **T4-001**: Perform integration test with local RabbitMQ and MinIO.
- [x] **T4-002**: Verify Scalar documentation renders correctly at `/geometry/scalar`.
- [x] **T4-003**: **[Scaling]** Configure KEDA ScaledObject and verify horizontal pod autoscaling based on RabbitMQ queue depth.
- [x] **T4-004**: Run `mypy` and `ruff` to ensure code quality compliance.