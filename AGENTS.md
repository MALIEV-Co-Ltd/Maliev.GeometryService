# AGENTS.md - Developer Guide for Maliev Geometry Service

> **Workspace root** `B:\maliev` contains **41 independent git repos**. Each `Maliev.*` folder and `maliev-gitops` is its own repo. There is no single repo at the workspace root. Always work within the target service directory.

---

## Build, Test & Lint Commands

All commands run from `B:\maliev\Maliev.GeometryService`.

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
  - `JWT_PUBLIC_KEY`: Base64-encoded RSA public key PEM for validating bearer tokens on HTTP analysis/debug endpoints. Required outside Development/Testing.
  - `JWT_SECURITY_KEY`: HMAC-SHA256 fallback key — used when `JWT_PRIVATE_KEY` is not set. Injected automatically by Aspire.
  - `JWT_ISSUER`: JWT issuer claim (default: `https://api.maliev.com`). Injected automatically by Aspire.
  - `JWT_AUDIENCE`: JWT audience claim (default: `https://api.maliev.com`). Injected automatically by Aspire.
  - `GEOMETRY_REQUIRE_AUTH`: Defaults to `true`; do not disable outside local troubleshooting.

### Testing
```bash
poetry run pytest                       # Run all tests
poetry run pytest tests/test_geometry.py::test_analyze_step  # Single test
poetry run pytest --cov=src             # With coverage
```

### Code Quality (Linting & Typing)
```bash
poetry run ruff check .                 # Lint
poetry run ruff format .                # Format
poetry run mypy src                     # Type check (strict mode)
```

---

## Code Style & Conventions

### Python (GeometryService only)
- **Types**: Mandatory type hints, Python 3.10+ syntax (`str | None`, `list[int]`)
- **Naming**: `PascalCase` classes, `snake_case` functions/variables, `UPPER_SNAKE_CASE` constants
- **Imports**: Absolute imports from `src.*`, alphabetized within groups
- **Pydantic V2**: `BaseModel` with `ConfigDict(alias_generator=to_camel)` for MassTransit compatibility
- **Async**: All I/O must be async; CPU-bound geometry work uses `ProcessPoolExecutor`

### Import Conventions
- **Order**:
  1. Standard library imports
  2. Third-party library imports
  3. Local `src` imports
- **Formatting**: Use absolute imports (e.g., `from src.core.config import settings`). Avoid relative imports.
- **Cleanliness**: Alphabetize imports within each group.

### Async/Await & Concurrency
- **I/O Bound**: Always use `async`/`await` for network (RabbitMQ, HTTP) and filesystem operations.
- **CPU Bound**: Geometry analysis (Trimesh/GMSH) is CPU-heavy. Offload these tasks to `ProcessPoolExecutor` using `loop.run_in_executor` to avoid blocking the event loop.
- **Lifespan**: Use FastAPI's `lifespan` context manager for managing background tasks and resource cleanup.

### Error Handling
- **Specific Exceptions**: Avoid bare `except:`. Catch specific errors like `ValueError`, `aio_pika.exceptions.AMQPError`, etc.
- **Error Codes**: When raising exceptions for analysis failures, use standard error codes:
  - `FILE_CORRUPT`: File cannot be parsed.
  - `MULTI_BODY_ERROR`: File contains multiple disconnected bodies where one was expected.
  - `SIZE_LIMIT_EXCEEDED`: File exceeds `MAX_FILE_SIZE_MB`.
  - `SYSTEM_ERROR`: Unexpected infrastructure failure.

### Logging & Observability
- **Logger**: Initialize via `logger = logging.getLogger(__name__)`.
- **Structured Logging**: Include context in `extra` dictionary for easier querying in log aggregators.
  - Example: `logger.info("Processing file", extra={"file_id": id, "extension": ext})`.
- **Tracing**: Wrap critical logical blocks with OpenTelemetry spans using the `tracer` from `src.core.observability`.

### Pydantic & Data Models
- **V2 Syntax**: Use Pydantic V2 features (`BaseModel`, `model_validator`, `Field`).
- **CamelCase Compatibility**: Integration with .NET/MassTransit requires CamelCase JSON.
  - Models should use `ConfigDict(alias_generator=to_camel, populate_by_name=True)`.
  - Use `model_dump_json(by_alias=True)` when publishing events to RabbitMQ.
- **Envelopes**: Use `MassTransitEnvelope` base class for all outgoing events.

---

## Banned Libraries (Build Will Fail)

| Banned | Use Instead |
|--------|-------------|
| AutoMapper | Manual mapping extensions |
| FluentValidation | DataAnnotations or manual validation |
| FluentAssertions | Standard xUnit `Assert.*` |
| Swashbuckle/Swagger | Scalar (at `/{service}/scalar`) |
| InMemoryDatabase (EF Core) | Testcontainers with real PostgreSQL |

---

## Testing Rules

- **Framework**: xUnit with standard `Assert` (`Assert.Equal`, `Assert.NotNull`, etc.)
- **Naming**: `MethodName_StateUnderTest_ExpectedBehavior` or `HTTP_METHOD_Path_Scenario_ExpectedStatus`
- **Coverage**: Minimum 80% per service
- **Integration tests**: `BaseIntegrationTestFactory<TProgram, TDbContext>` with Testcontainers (PostgreSQL, Redis, RabbitMQ). Never InMemoryDatabase
- **System tests** (Tier 3): `AspireTestFixture` with `[Collection("AspireDomainTests")]` — shared AppHost, never one per class
- **Eventual consistency**: Use `TestHelpers.WaitForAsync`. Never `Task.Delay`
- **MassTransit consumers**: Must have consumer tests using `AddMassTransitTestHarness()`

---

## Mandatory Rules

- **`TreatWarningsAsErrors = true`**: Zero warnings allowed. No suppression
- **`[RequirePermission("domain.resources.action")]`**: On all endpoints, not plain `[Authorize]`
- **API versioning**: All routes versioned (`v1/`)
- **Service prefix**: Routes prefixed with service domain (e.g., `/auth`, `/customer`, `/job`)
- **Scalar docs**: Configured at `/{service}/scalar`
- **Secrets**: Never hardcoded. Use GCP Secret Manager or environment variables
- **HTTP authentication**: Keep `/geometry/liveness`, `/geometry/readiness`, `/geometry/aspire-liveness`, `/geometry/scalar`, and `/geometry/openapi/v1.json` public for probes/docs. All analysis, cleanup, and debug endpoints require a bearer token validated with `JWT_PUBLIC_KEY` outside Development/Testing.
- **Async/await**: All the way down. Pass `CancellationToken`
- **EF Core Design package**: Only in Infrastructure project, never in Api
- **PostgreSQL xmin**: Shadow property only — `entity.Property<uint>("xmin").HasColumnType("xid").IsRowVersion()`. Never add entity property
- **Temporary files**: Generate in `/temp` folder, clean up afterwards

---

## Architecture & Messaging

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

## Preview Image Generation

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
docker-compose -f docker-compose.test.yml build test
docker-compose -f docker-compose.test.yml run --rm test

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

---

## Verification Checklist
- [ ] Code is formatted with `ruff format`.
- [ ] All linting issues resolved via `ruff check`.
- [ ] Type checking passes: `poetry run mypy src`.
- [ ] All tests pass: `poetry run pytest`.
- [ ] No hardcoded secrets (use `src.core.config.settings`).
- [ ] RabbitMQ event keys match expected CamelCase aliases.

---

## Git Rules

- Each `Maliev.*` folder is an independent git repo. `cd` into it before git commands
- **Commit early and often** after every meaningful unit of work. Do not accumulate changes
- **Never use `git checkout` to restore files** — commit first, then `git revert` or `git reset --soft`
- Feature branches merged to `develop` via PR. Do not push without being asked
