# Maliev.GeometryService Development Guidelines

Auto-generated from all feature plans. Last updated: 2025-01-02

## Active Technologies

- Python 3.11+
- FastAPI
- trimesh (Geometry Kernel)
- aio-pika (RabbitMQ)
- Pydantic v2
- pytest

## Project Structure

```text
src/
  core/
  consumers/
  infrastructure/
tests/
```

## Commands

# Add commands for Python
- Install: `poetry install`
- Run: `poetry run python -m src.main`
- Test: `poetry run pytest`

## Code Style

Python: Follow PEP 8, strict typing (mypy), and ruff linting.

## Recent Changes

- 001-geometry-service: Initial setup with Python 3.11, trimesh, and aio-pika. Added support for 3MF, STEP (.step, .stp), and IGES (.igs, .iges) file formats.

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
