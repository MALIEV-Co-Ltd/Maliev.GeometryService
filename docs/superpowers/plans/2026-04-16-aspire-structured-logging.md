# Aspire Structured Logging Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route all GeometryService Python logs through OTLP to the Aspire Dashboard structured-log view instead of only appearing as plain-text console output.

**Architecture:** Two independent bugs must be fixed in two repos. Bug A is in the Aspire AppHost (C#): the OTel collector hook that rewrites `OTEL_EXPORTER_OTLP_ENDPOINT` at container-start only fires when the key already exists in Aspire's env-var registry — but the GeometryService Docker registration never seeds the key, so the hook silently skips it and the endpoint stays empty. Bug B is in the Python service: even with a valid endpoint the root logger level defaults to WARNING, dropping every INFO log before it reaches the OTel handler.

**Tech Stack:** Python 3.12, opentelemetry-sdk 1.29, FastAPI/Uvicorn, .NET 10 Aspire AppHost

---

## Background: Why logs appear plain-text today

```
AppHost.cs:858            → geometryService has NO .WithEnvironment("OTEL_EXPORTER_OTLP_ENDPOINT", ...)
                                ↓
OTelCollectorHook:56      → ContainsKey("OTEL_EXPORTER_OTLP_ENDPOINT") == FALSE → hook skips
                                ↓
container starts          → OTEL_EXPORTER_OTLP_ENDPOINT=""  (Dockerfile default, never updated)
                                ↓
main.py:26                → `if not settings.OTEL_EXPORTER_OTLP_ENDPOINT:` → TRUE
                                ↓
logging.basicConfig       → plain-text stdout handler only; no OTLP
```

Even if the endpoint were populated, `observability.py:79` attaches an OTel handler to the root logger but never calls `root_logger.setLevel(logging.INFO)`. Root logger stays at WARNING, INFO logs are silently dropped before reaching the handler.

---

## File Map

| File | Change |
|---|---|
| `B:/maliev/Maliev.Aspire/Maliev.Aspire.AppHost/AppHost.cs` | Seed `OTEL_EXPORTER_OTLP_ENDPOINT` env key so the OTel collector hook updates it |
| `B:/maliev/Maliev.GeometryService/src/core/observability.py` | Set root logger level to INFO when OTLP handler is attached |

No new files. No dependency changes.

---

## Task 1: Seed `OTEL_EXPORTER_OTLP_ENDPOINT` in AppHost for GeometryService

**Files:**
- Modify: `B:/maliev/Maliev.Aspire/Maliev.Aspire.AppHost/AppHost.cs:858-873`

The OTel collector hook (in `OpenTelemetryCollectorResourceBuilderExtensions.cs:55-61`) runs a `BeforeResourceStartedEvent` callback that checks:
```csharp
if (context.EnvironmentVariables.ContainsKey(OtelExporterOtlpEndpoint))
{
    context.EnvironmentVariables[OtelExporterOtlpEndpoint] = otlpEndpoint;
}
```
Dockerfile `ENV` instructions are baked into the image layer and are NOT visible to Aspire's `EnvironmentCallbackAnnotation` context. The only way to seed the key is via an explicit `.WithEnvironment(...)` call in the AppHost registration. Adding an empty-string seed is enough — the hook replaces the value with the live collector gRPC endpoint before the container starts.

- [ ] **Step 1: Add the seeded env var to the geometryService registration**

Find this block in `AppHost.cs` (currently lines 858-873):
```csharp
var geometryService = builder.AddDockerfile("GeometryService", "../../Maliev.GeometryService")
    .WithReference(infrastructure.RabbitMQ)
    .WaitFor(infrastructure.RabbitMQ)
    .WithReference(uploadService)
    .WaitFor(uploadService)
    .WithEnvironment("RABBITMQ_URI", infrastructure.RabbitMQ)
    .WithEnvironment("UPLOAD_SERVICE_URL", uploadService.GetEndpoint("http"))
    .WithEnvironment("JWT_PRIVATE_KEY", config.JwtPrivateKey)
    .WithEnvironment("JWT_SECURITY_KEY", config.JwtSecurityKey)
    .WithEnvironment("JWT_ISSUER", config.JwtIssuer)
    .WithEnvironment("JWT_AUDIENCE", config.JwtAudience)
    .WithExternalHttpEndpoints()
    .WithHttpEndpoint(port: 8081, targetPort: 8081, env: "PORT")
    .WithUrlForEndpoint("http", u => { u.Url = "/geometry/scalar"; u.DisplayText = "Scalar Documentation"; })
    .WithHttpHealthCheck("/geometry/aspire-liveness");
```

Replace it with (add one `.WithEnvironment` line after `JWT_AUDIENCE`):
```csharp
var geometryService = builder.AddDockerfile("GeometryService", "../../Maliev.GeometryService")
    .WithReference(infrastructure.RabbitMQ)
    .WaitFor(infrastructure.RabbitMQ)
    .WithReference(uploadService)
    .WaitFor(uploadService)
    .WithEnvironment("RABBITMQ_URI", infrastructure.RabbitMQ)
    .WithEnvironment("UPLOAD_SERVICE_URL", uploadService.GetEndpoint("http"))
    .WithEnvironment("JWT_PRIVATE_KEY", config.JwtPrivateKey)
    .WithEnvironment("JWT_SECURITY_KEY", config.JwtSecurityKey)
    .WithEnvironment("JWT_ISSUER", config.JwtIssuer)
    .WithEnvironment("JWT_AUDIENCE", config.JwtAudience)
    .WithEnvironment("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    .WithExternalHttpEndpoints()
    .WithHttpEndpoint(port: 8081, targetPort: 8081, env: "PORT")
    .WithUrlForEndpoint("http", u => { u.Url = "/geometry/scalar"; u.DisplayText = "Scalar Documentation"; })
    .WithHttpHealthCheck("/geometry/aspire-liveness");
```

- [ ] **Step 2: Build the AppHost to confirm zero errors**

```bash
cd B:/maliev/Maliev.Aspire
dotnet build Maliev.Aspire.AppHost/Maliev.Aspire.AppHost.csproj
```
Expected: `Build succeeded. 0 Warning(s). 0 Error(s).`

- [ ] **Step 3: Commit**

```bash
cd B:/maliev/Maliev.Aspire
git add Maliev.Aspire.AppHost/AppHost.cs
git commit -m "fix(apphost): seed OTEL_EXPORTER_OTLP_ENDPOINT for GeometryService so OTel collector hook fires"
```

---

## Task 2: Fix root logger level in `observability.py`

**Files:**
- Modify: `B:/maliev/Maliev.GeometryService/src/core/observability.py:73-83`

When `OTEL_EXPORTER_OTLP_ENDPOINT` is configured, `setup_observability` attaches a `LoggingHandler` to the root logger but never calls `root_logger.setLevel(logging.INFO)`. Python's root logger defaults to `WARNING`. Every INFO log emitted by child loggers (`src.main`, `src.consumers.*`, `src.core.*`, `uvicorn`, etc.) propagates up to root, hits the `WARNING` gate, and is **silently discarded** before reaching the OTel handler.

The `setLevel(logging.INFO)` call must go on the **root logger** (not on the handler). The handler already has `level=logging.NOTSET` which means it accepts everything; the problem is upstream at the root logger gate.

- [ ] **Step 1: Write the failing test**

Create or open `tests/test_observability.py` and add:

```python
import logging
import importlib
import unittest.mock as mock

from src.core import observability as obs_module


def _reset_observability():
    """Reset the module-level _is_configured flag between tests."""
    obs_module._is_configured = False
    # Remove any OTel handlers that a previous run may have added
    root = logging.getLogger()
    from opentelemetry.sdk._logs import LoggingHandler
    root.handlers = [h for h in root.handlers if not isinstance(h, LoggingHandler)]


def test_root_logger_level_set_to_info_when_otlp_configured(monkeypatch):
    """Root logger must be at INFO level when an OTLP endpoint is configured.

    Before the fix, root logger stayed at WARNING (default). Every INFO log
    from child loggers was silently dropped before reaching the OTel handler.
    """
    _reset_observability()

    # Patch settings so the OTLP branch is taken
    monkeypatch.setattr(obs_module.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setattr(obs_module.settings, "SERVICE_NAME", "test-service")

    # Stub out the real OTLP exporters so we don't need a live collector
    with mock.patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"), \
         mock.patch("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter"), \
         mock.patch("opentelemetry.exporter.otlp.proto.grpc._log_exporter.OTLPLogExporter"):
        obs_module.setup_observability()

    root_logger = logging.getLogger()
    assert root_logger.level == logging.INFO, (
        f"Root logger level should be INFO ({logging.INFO}) "
        f"but got {root_logger.level} ({logging.getLevelName(root_logger.level)})"
    )

    _reset_observability()
```

- [ ] **Step 2: Run the test to confirm it fails before the fix**

```bash
cd B:/maliev/Maliev.GeometryService
poetry run pytest tests/test_observability.py::test_root_logger_level_set_to_info_when_otlp_configured -v
```
Expected: `FAILED` — `AssertionError: Root logger level should be INFO (20) but got 30 (WARNING)`

- [ ] **Step 3: Apply the one-line fix in `observability.py`**

In `src/core/observability.py`, the current block ending at line ~83 reads:
```python
        # Configure root logger to include the OTel handler
        root_logger = logging.getLogger()
        if not any(isinstance(h, LoggingHandler) for h in root_logger.handlers):
            root_logger.addHandler(otel_handler)

        _is_configured = True
```

Replace with:
```python
        # Configure root logger to include the OTel handler
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)   # ← root defaults to WARNING; must be INFO
        if not any(isinstance(h, LoggingHandler) for h in root_logger.handlers):
            root_logger.addHandler(otel_handler)

        _is_configured = True
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
cd B:/maliev/Maliev.GeometryService
poetry run pytest tests/test_observability.py::test_root_logger_level_set_to_info_when_otlp_configured -v
```
Expected: `PASSED`

- [ ] **Step 5: Run the full test suite to check for regressions**

```bash
cd B:/maliev/Maliev.GeometryService
poetry run pytest -x -q
```
Expected: all previously passing tests still pass.

- [ ] **Step 6: Commit**

```bash
cd B:/maliev/Maliev.GeometryService
git add src/core/observability.py tests/test_observability.py
git commit -m "fix(observability): set root logger level to INFO so OTel handler receives INFO logs"
```

---

## Verification

After both fixes are deployed and Aspire is restarted:

1. Open the Aspire Dashboard → **GeometryService → Structured Logs** tab
2. Startup logs (`Faulthandler enabled`, `Starting Geometry Service background consumer…`, `Successfully connected to RabbitMQ`, etc.) should now appear as structured log entries with `level`, `message`, `service.name = maliev-geometryservice`, and timestamp fields — not as raw text
3. The **Console** tab may still show uvicorn's own output lines (`INFO: Application startup complete.`) — that is expected and acceptable; those come from uvicorn's internal stream handler

---

## Why no other changes are needed

| Concern | Why it's not a problem |
|---|---|
| Uvicorn's own log lines (`INFO: Started server process`) | `uvicorn` logger has `propagate=True` (default). Once root logger is at INFO, these propagate to the OTel handler automatically |
| Worker subprocess logs (multiprocessing DFM) | Workers inherit the env but not the logging handlers; their logs are emitted to stdout and captured by Aspire's console view. This is acceptable — subprocess OTLP logging would require a separate logging queue and is out of scope |
| `logging.basicConfig` in `main.py` | The condition `if not settings.OTEL_EXPORTER_OTLP_ENDPOINT:` is correct. Once AppHost seeds the endpoint, this branch is never taken in Aspire. Leave it as-is for local dev |
| `uvicorn.access` log spam | Already suppressed via `logging.getLogger("uvicorn.access").setLevel(logging.WARNING)` in `observability.py:31` |
