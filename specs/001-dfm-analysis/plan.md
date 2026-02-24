# Implementation Plan: DFM Analysis Integration

**Branch**: `001-dfm-analysis` | **Date**: 2026-02-21 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-dfm-analysis/spec.md`

## Summary

Extend the existing `GeometryProcessor.analyze_bytes()` pipeline to include DFM (Design for Manufacturability) analysis. The feature adds thin wall detection via ray-casting and overhang detection via normal angle computation, returning results in a new `DfmReport` embedded within `FileAnalyzedEvent`. DFM analysis is best-effort and must not fail the primary geometry metrics flow.

## Technical Context

**Language/Version**: Python 3.10+
**Primary Dependencies**: FastAPI, trimesh (with all extras), numpy, pydantic v2, aio-pika, gmsh
**Storage**: N/A (stateless service)
**Testing**: pytest with pytest-asyncio
**Target Platform**: Linux server (RabbitMQ consumer via AMQP)
**Project Type**: web-service (background worker consuming AMQP events)
**Performance Goals**: DFM analysis < 5 seconds for meshes under 100k triangles
**Constraints**: DFM failures must not affect geometry analysis success rate (0% impact)
**Scale/Scope**: 500 sample points per mesh for thin wall detection; single mesh per event

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution file is a template placeholder. Using AGENTS.md and project conventions:

- [x] **Type Hints**: All functions must have explicit type hints (mypy strict mode)
- [x] **Error Handling**: Catch specific exceptions, not bare `except:`
- [x] **Graceful Degradation**: DFM failures return zeros/null, don't propagate
- [x] **Units**: Length in mm, Area in cm², Volume in cm³
- [x] **Testing**: All tests must pass before merge

## Project Structure

### Documentation (this feature)

```text
specs/001-dfm-analysis/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
└── contracts/           # Phase 1 output (message schema)
```

### Source Code (repository root)

```text
src/
├── core/
│   ├── config.py        # Settings
│   ├── geometry.py      # GeometryProcessor, GeometryMetrics (MODIFY)
│   ├── schemas.py       # AMQP message schemas (MODIFY)
│   └── dfm.py           # DfmAnalyzer, DfmReport (CREATE)
├── consumers/
│   └── upload_consumer.py
├── infrastructure/
│   └── ...
└── main.py

tests/
├── test_geometry.py     # Existing tests
└── test_dfm.py          # DFM unit tests (CREATE)
```

**Structure Decision**: Single project structure. New `dfm.py` module added to `src/core/` alongside existing geometry processing logic.

## Complexity Tracking

No constitution violations. Feature extends existing module with new capability.

## Phase 0: Research

See `research.md` for findings on:
- Trimesh ray-casting API for thickness measurement
- Normal angle computation for overhang detection
- Error handling patterns for best-effort analysis

## Phase 1: Design

See `data-model.md` for:
- `DfmReport` Pydantic model definition
- `GeometryMetrics` extension
- Message schema changes

See `contracts/` for:
- Updated `FileAnalyzedEvent` message schema
