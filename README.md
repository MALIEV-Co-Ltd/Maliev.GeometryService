# Maliev Geometry Service

[![Build Status](https://img.shields.io/badge/Build-Passing-success)](https://github.com/ORGANIZATION/Maliev.GeometryService)
[![Python Version](https://img.shields.io/badge/Python-3.11+-blue)](https://www.python.org/downloads/)
[![Queue](https://img.shields.io/badge/Queue-RabbitMQ-orange)](https://www.rabbitmq.com/)

Dedicated 3D geometry analysis service providing automated processing of 3D CAD and mesh files.

**Role in MALIEV Architecture**: Analyzes uploaded 3D files to extract manufacturing metrics such as volume, surface area, and bounding boxes. It operates as an asynchronous worker consuming file upload events and publishing analysis results.

---

## 🏗️ Architecture & Tech Stack

- **Framework**: Python 3.10+ with FastAPI
- **Geometry Kernel**: Trimesh + GMSH (for CAD formats)
- **Messaging**: RabbitMQ via aio-pika
- **API Documentation**: OpenAPI 3.1 (Swagger)
- **Observability**: Prometheus Metrics

---

## ⚖️ Constitution Rules

This service adheres to the platform development mandates adapted for Python:

### Technical Standards
- ✅ **Strict Typing**: All code must use Python type hints and pass `mypy` checks.
- ✅ **Linting**: Adherence to PEP 8 via `ruff`.
- ✅ **No Secrets in Code**: Configuration via environment variables only.
- ✅ **Authenticated HTTP API**: Analysis/debug endpoints require a platform bearer token.
- ✅ **Async First**: All I/O operations must be asynchronous.

### Mandatory Practices
- ✅ **IAM Integration**: Permissions naming: `geometry.{resource}.{action}`.
- ✅ **Health Probes**: Standardized `/liveness` and `/readiness` endpoints.
- ✅ **Event Driven**: Consumes `maliev.uploadservice.v1.upload.completed` events.

---

## ✨ Key Features

- **Automated Analysis**: Automatic extraction of Volume (cm³), Surface Area (cm²), and Bounding Box (mm).
- **CAD Support**: Full support for STEP (.step, .stp) and IGES (.igs, .iges) formats via GMSH.
- **Mesh Support**: High-performance analysis of STL, OBJ, and 3MF files.
- **Topological Validation**: Checks for manifold status and mesh integrity.
- **Scalable Workers**: Designed to run as multiple consumer instances for high throughput.
- **6-Sided Preview Images**: Generates 6-view renders (front, back, left, right, top, bottom) of 3D geometry for visual inspection in Job Tickets.

---

## 🖼️ Preview Image Generation

The service generates 6-sided preview images of 3D geometry for use in Job Ticket PDFs. These are rendered headlessly using PyVista with OSMesa.

### Rendering Style

The preview renderer uses Shapr3D/Onshape-style CAD rendering:
- **Matte gray material** — no specular highlights
- **Smooth shading** — interpolated normals for curved surfaces
- **Soft 3-point lighting** — key, fill, and ambient lights for even illumination
- **No edge lines** — avoids exposing STL mesh tessellation artifacts

### Generating Preview Images for Review

```bash
# Windows
generate_previews.bat

# Linux/Bash
./generate_previews.sh
```

This builds the Docker test container, runs the preview generation test, and copies output to `previews/` folder.

See [AGENTS.md](./AGENTS.md) for detailed instructions.

---

## 🚀 Quick Start

### Prerequisites
- Python 3.10+
- Poetry (Package Manager)
- GMSH (for CAD processing)
- Docker Desktop (for RabbitMQ)

### Local Development Setup

1. **Clone the repository**
```bash
git clone https://github.com/ORGANIZATION/Maliev.GeometryService.git
cd Maliev.GeometryService
```

2. **Install Dependencies**
```bash
poetry install
```

3. **Configure Environment**
```bash
# Set RabbitMQ connection string
$env:AMQP_URL="YOUR_RABBITMQ_URL"

# Required for HTTP endpoint authentication outside tests/dev fallback
$env:JWT_PUBLIC_KEY="<base64-rsa-public-pem>"
$env:JWT_ISSUER="https://api.maliev.com"
$env:JWT_AUDIENCE="https://api.maliev.com"
```

4. **Run the Service**
```bash
poetry run python -m src.main
```

The service will be available at `http://localhost:8000/geometry`.
Health and Scalar/OpenAPI endpoints are public for probes and documentation. Geometry analysis, cleanup, and debug endpoints require `Authorization: Bearer <platform-jwt>`. Development/Testing may use `JWT_SECURITY_KEY`; production must provide `JWT_PUBLIC_KEY`.

---

## 📡 Messaging interface

This service primarily operates via RabbitMQ events.

| Exchange | Routing Key | Description |
|----------|-------------|-------------|
| `maliev.uploadservice` | `v1.upload.completed` | Consumes uploaded file events |
| `maliev.geometryservice` | `v1.analysis.completed` | Publishes successful analysis results |
| `maliev.geometryservice` | `v1.analysis.failed` | Publishes analysis failure details |
| `maliev.geometryservice` | `v1.preview-images.generated` | Publishes 6-sided preview image paths (after analysis) |

---

## 🏥 Health & Monitoring

Standardized health probes:
- **Liveness**: `GET /geometry/liveness`
- **Readiness**: `GET /geometry/readiness`
- **Metrics**: `GET /geometry/metrics`

HTTP analysis endpoints:

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/geometry/uploads/{upload_id}/quality-check` | Bearer JWT | Phase 1 STL/CAD quality check |
| `POST` | `/geometry/uploads/{upload_id}/dfm/{process_code}` | Bearer JWT | Phase 2 process-specific DFM analysis |
| `DELETE` | `/geometry/uploads/{upload_id}` | Bearer JWT | Clears cached upload analysis data |

---

## 🧪 Testing

```bash
# Run unit and integration tests
poetry run pytest
```

---

## 📦 Deployment

- **Docker Image**: `REGION-docker.pkg.dev/PROJECT_ID/REPOSITORY/maliev-geometryservice:{sha}`
- **Environments**: Development, Staging, Production

---

## 📄 License

Proprietary - © 2025 MALIEV Co., Ltd. All rights reserved.
