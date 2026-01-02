# Research & Decisions: Geometry Analysis Service

## Technology Decisions

### 1. Language & Runtime
- **Decision**: Python 3.11+
- **Rationale**: Python is the dominant ecosystem for scientific computing and geometry processing. Versions 3.11+ offer significant performance improvements.
- **Alternatives Considered**: C++ (too complex for MVP), C# (limited geometry libraries compared to Python).

### 2. Geometry Kernel
- **Decision**: `trimesh` (with `numpy`, `scipy`, `networkx`, and `gmsh`/`meshio` for imports)
- **Rationale**: `trimesh` is a mature, feature-rich library that abstracts away the complexity of mesh analysis (volume, manifold checks, convex hulls). It supports the required formats (STL, OBJ, STEP via loaders).
- **Alternatives Considered**: `Open3D` (more focused on point clouds), `PyMesh` (complex build dependencies).

### 3. Messaging Client
- **Decision**: `aio-pika`
- **Rationale**: Provides a robust, fully async interface for RabbitMQ, integrating well with Python's `asyncio` event loop.
- **Alternatives Considered**: `pika` (synchronous/blocking by default, harder to mix with async I/O).

### 4. Web/App Framework
- **Decision**: `FastAPI`
- **Rationale**: Used primarily for providing Health/Metrics endpoints and managing the application lifecycle. It's modern, fast, and type-safe.

### 5. Dependency Management
- **Decision**: `Poetry`
- **Rationale**: Provides deterministic dependency resolution and simpler packaging than raw `pip`/`requirements.txt`.

## Integration Patterns

- **Tessellation**: Processing STEP files requires tessellation (converting NURBS/BREP to Mesh). `trimesh` delegates this to `gmsh` or `meshio`. The Docker image must include these system dependencies.
- **Concurrency**: CPU-bound geometry tasks must be offloaded to a `ProcessPoolExecutor` (or `ThreadPoolExecutor` if GIL is released by C-extensions) to prevent blocking the `asyncio` loop handling heartbeats and I/O.
