# AGENTS.md - Developer Guide for Maliev Geometry Service

## 🛠 Build & Development Commands

This project uses **Poetry** for dependency management and **Pytest** for testing.

### Environment Setup
- **Install Dependencies**: `poetry install`
- **Activate Virtualenv**: `poetry shell`

### Running the Service
- **Start Worker**: `poetry run python -m src.main`
- **Environment Variables**:
  - `AMQP_URL`: RabbitMQ connection string (e.g., `amqp://guest:guest@localhost:5672/`)
  - `MAX_FILE_SIZE_MB`: Maximum allowed file size for analysis (default: 100)
  - `OTEL_EXPORTER_OTLP_ENDPOINT`: OpenTelemetry collector endpoint
  - `UPLOAD_SERVICE_URL`: UploadService endpoint for artifact uploads (e.g., `http://uploadservice:8080`) — **Required for uploading preview images**. Injected automatically by Aspire.
  - `JWT_PRIVATE_KEY`: Base64-encoded RSA private key PEM for RS256 signing — **preferred**. Injected automatically by Aspire.
  - `JWT_SECURITY_KEY`: HMAC-SHA256 fallback key — used when `JWT_PRIVATE_KEY` is not set. Injected automatically by Aspire.
  - `JWT_ISSUER`: JWT issuer claim (default: `https://api.maliev.com`). Injected automatically by Aspire.
  - `JWT_AUDIENCE`: JWT audience claim (default: `https://api.maliev.com`). Injected automatically by Aspire.

### Testing
- **Run All Tests**: `poetry run pytest`
- **Run Single File**: `poetry run pytest tests/test_geometry.py`
- **Run Specific Test Case**: `poetry run pytest tests/test_geometry.py::test_analyze_step`
- **Run with stdout enabled**: `poetry run pytest -s`
- **Run with Coverage**: `poetry run pytest --cov=src`

### Code Quality (Linting & Typing)
- **Lint Check (Ruff)**: `poetry run ruff check .`
- **Lint Fix**: `poetry run ruff check . --fix`
- **Format Check**: `poetry run ruff format --check .`
- **Format Apply**: `poetry run ruff format .`
- **Type Check (Mypy)**: `poetry run mypy src` (Strict mode is mandatory)

---

## 📐 Code Style Guidelines

### 1. Type Hinting & Signatures
- **Mandatory Types**: All functions, methods, and variables must have explicit type hints.
- **Modern Syntax**: Use Python 3.10+ union types (e.g., `str | None` instead of `Optional[str]`).
- **Generic Collections**: Use built-in generics (e.g., `list[int]`, `dict[str, Any]`).
- **Mypy Compliance**: All code must pass `mypy src --strict`.

### 2. Import Conventions
- **Order**:
  1. Standard library imports
  2. Third-party library imports
  3. Local `src` imports
- **Formatting**: Use absolute imports (e.g., `from src.core.config import settings`). Avoid relative imports.
- **Cleanliness**: Alphabetize imports within each group.

### 3. Naming Conventions
- **Classes**: `PascalCase` (e.g., `GeometryProcessor`).
- **Functions/Methods**: `snake_case` (e.g., `process_file`).
- **Variables/Constants**: `snake_case` for variables, `UPPER_SNAKE_CASE` for constants.
- **Private Members**: Prefix with a single underscore (e.g., `_calculate_volume`).

### 4. Async/Await & Concurrency
- **I/O Bound**: Always use `async`/`await` for network (RabbitMQ, HTTP) and filesystem operations.
- **CPU Bound**: Geometry analysis (Trimesh/GMSH) is CPU-heavy. Offload these tasks to `ProcessPoolExecutor` using `loop.run_in_executor` to avoid blocking the event loop.
- **Lifespan**: Use FastAPI's `lifespan` context manager for managing background tasks and resource cleanup.

### 5. Error Handling
- **Specific Exceptions**: Avoid bare `except:`. Catch specific errors like `ValueError`, `aio_pika.exceptions.AMQPError`, etc.
- **Error Codes**: When raising exceptions for analysis failures, use standard error codes:
  - `FILE_CORRUPT`: File cannot be parsed.
  - `MULTI_BODY_ERROR`: File contains multiple disconnected bodies where one was expected.
  - `SIZE_LIMIT_EXCEEDED`: File exceeds `MAX_FILE_SIZE_MB`.
  - `SYSTEM_ERROR`: Unexpected infrastructure failure.

### 6. Logging & Observability
- **Logger**: Initialize via `logger = logging.getLogger(__name__)`.
- **Structured Logging**: Include context in `extra` dictionary for easier querying in log aggregators.
  - Example: `logger.info("Processing file", extra={"file_id": id, "extension": ext})`.
- **Tracing**: Wrap critical logical blocks with OpenTelemetry spans using the `tracer` from `src.core.observability`.

### 7. Pydantic & Data Models
- **V2 Syntax**: Use Pydantic V2 features (`BaseModel`, `model_validator`, `Field`).
- **CamelCase Compatibility**: Integration with .NET/MassTransit requires CamelCase JSON.
  - Models should use `ConfigDict(alias_generator=to_camel, populate_by_name=True)`.
  - Use `model_dump_json(by_alias=True)` when publishing events to RabbitMQ.
- **Envelopes**: Use `MassTransitEnvelope` base class for all outgoing events.

---

## 🏗 Architecture & Messaging

### Data Flow
1. **Trigger**: Consumes `FileUploadedEvent` from `maliev.uploadservice.v1.upload.completed`.
2. **Download**: Fetches file from storage via `HttpDownloadService` (with retry logic).
3. **Analyze**: Processes geometry in a sub-process (GMSH for CAD, Trimesh for Mesh).
4. **Publish**: Sends `FileAnalyzedEvent` or `FileAnalysisFailedEvent` to `maliev.events` exchange.

### Unit Standards
- **Length**: Millimeters (mm).
- **Volume**: Exported as Cubic Centimeters (cm³).
- **Surface Area**: Exported as Square Centimeters (cm²).
- **Bounding Box**: Exported as Millimeters (mm).

### Key Libraries
- **FastAPI**: Web framework and API.
- **Trimesh**: Mesh processing and metrics.
- **GMSH**: CAD (STEP/IGES) tessellation.
- **aio-pika**: Asynchronous RabbitMQ client.
- **OpenTelemetry**: Tracing and metrics.

---

## 🚦 Verification Checklist
- [ ] Code is formatted with `ruff format`.
- [ ] All linting issues resolved via `ruff check`.
- [ ] Type checking passes: `poetry run mypy src`.
- [ ] All tests pass: `poetry run pytest`.
- [ ] No hardcoded secrets (use `src.core.config.settings`).
- [ ] RabbitMQ event keys match expected CamelCase aliases.


## Database & EF Core — Mandatory Rules

### EF Core Design Package
- ❌ `Microsoft.EntityFrameworkCore.Design` MUST NOT be in Api projects
- ✅ It belongs ONLY in the Infrastructure (or Data) project where migrations live
- Migration commands must target Infrastructure as both project and startup-project (since EF Core Design package is in Infrastructure):
  ```
  dotnet ef migrations add <Name> --project Maliev.<Domain>Service.Infrastructure --startup-project Maliev.<Domain>Service.Infrastructure
  ```

### PostgreSQL xmin Concurrency — Mandatory Pattern
Use shadow property ONLY. Never add a Xmin/xmin property to domain entities.
```csharp
entity.Property<uint>("xmin").HasColumnType("xid").IsRowVersion();
```
- ❌ Never use `UseXminAsConcurrencyToken()` (removed in Npgsql EF v7)
- ❌ Never use entity property `public uint Xmin { get; set; }` or `public uint xmin { get; set; }`
- ❌ Never use `.Ignore(e => e.Xmin)` — remove the entity property instead

---

## 🎨 Generating Preview Images for Review

Preview images are 6-sided renders of 3D geometry (front, back, left, right, top, bottom) generated using PyVista with OSMesa for headless rendering.

### Running Preview Generation

**Windows:**
```bash
generate_previews.bat
```

**Linux/Bash:**
```bash
./generate_previews.sh
```

Both scripts:
1. Run the Docker test container with Xvfb
2. Execute the preview image test
3. Copy output from `test_output/` to `previews/`

### Manual Docker Commands

If you prefer to run manually:

```bash
# Build and run the test container
docker-compose -f docker-compose.test.yml build test
docker-compose -f docker-compose.test.yml run --rm test

# Copy output to accessible folder
mkdir -p previews
cp -r test_output/* previews/
# or on Windows:
xcopy /E /I /Y test_output previews
```

### Preview Output Location

Generated images are saved to:
- `test_output/` — raw test output (gitignored)
- `previews/` — copied images for viewing (gitignored)

### Rendering Style

The preview renderer uses Shapr3D/Onshape-style CAD rendering:
- **Matte gray material** — no specular highlights
- **Smooth shading** — interpolated normals for curved surfaces
- **Soft 3-point lighting** — key, fill, and ambient lights for even illumination
- **No edge lines** — avoids exposing STL mesh tessellation artifacts

### Test STL Model

The reference test model is `tests/assets/dice.stl` — a die with rounded corners and indentations for the dots. This model is used to validate the rendering quality.
