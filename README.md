# Maliev.GeometryService

Dedicated 3D geometry analysis service for the Maliev platform.

## Overview

This service provides automated processing of 3D files to extract manufacturing metrics:
- **Volume** (cm³)
- **Surface Area** (cm²)
- **Bounding Box** (AABB in mm)
- **Topological Validity** (Manifold status)

## Supported Formats

- **Mesh**: STL, OBJ, 3MF
- **CAD**: STEP (.step, .stp), IGES (.igs, .iges)

## Prerequisites

To process CAD formats (STEP/IGES) locally, you must have **GMSH** installed and available in your PATH.

### Installation

- **Windows**: 
  1. Download the GMSH binary from [gmsh.info](https://gmsh.info/bin/Windows/).
  2. Extract and add the `bin` folder to your System Environment Variables (PATH).
- **Ubuntu/Debian**: `sudo apt-get install gmsh`
- **macOS**: `brew install gmsh`

## Local Development

### Setup
```bash
poetry install
```

### Run
```bash
poetry run python -m src.main
```

### Test
```bash
poetry run pytest
```

## Architecture

The service operates as a RabbitMQ consumer:
1. Subscribes to `maliev.uploadservice.v1.upload.completed`.
2. Downloads the file from the provided URL.
3. Analyzes geometry using `trimesh` + `gmsh`.
4. Publishes `FileAnalyzedEvent` or `FileAnalysisFailedEvent`.
