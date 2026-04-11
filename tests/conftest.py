"""Shared test fixtures for multi-body testing."""

import pytest
import trimesh
import numpy as np


@pytest.fixture
def single_body_cube():
    """Single box mesh - baseline for comparison."""
    mesh = trimesh.creation.box([10, 10, 10])
    return mesh


@pytest.fixture
def two_body_meshes():
    """Two spatially separated boxes - basic multi-body test."""
    body1 = trimesh.creation.box([10, 10, 10])
    body2 = trimesh.creation.box([10, 10, 10])
    body2.apply_translation([25, 0, 0])  # Separated by 15mm
    return [body1, body2]


@pytest.fixture
def three_body_meshes():
    """Three boxes in different quadrants."""
    body1 = trimesh.creation.box([5, 5, 5])
    body2 = trimesh.creation.box([5, 5, 5])
    body2.apply_translation([20, 0, 0])
    body3 = trimesh.creation.box([5, 5, 5])
    body3.apply_translation([0, 20, 0])
    return [body1, body2, body3]


@pytest.fixture
def many_body_meshes():
    """12 bodies in grid pattern - stress test for performance."""
    bodies = []
    for i in range(3):
        for j in range(4):
            body = trimesh.creation.box([5, 5, 5])
            body.apply_translation([i * 15, j * 15, 0])
            bodies.append(body)
    return bodies


@pytest.fixture
def overlapping_body_meshes():
    """Two overlapping boxes - test intersection handling."""
    body1 = trimesh.creation.box([10, 10, 10])
    body2 = trimesh.creation.box([10, 10, 10])
    body2.apply_translation([5, 0, 0])  # Overlap by 5mm
    return [body1, body2]


@pytest.fixture
def mixed_watertight_bodies():
    """Mix of watertight and non-watertight bodies."""
    body1 = trimesh.creation.box([10, 10, 10])  # Watertight
    body2 = trimesh.creation.box([10, 10, 10])
    # Remove some faces to make non-watertight
    body2.faces = body2.faces[:-10]
    return [body1, body2]


@pytest.fixture
def multibody_stl_bytes_dict():
    """Pre-generated STL bytes dict for two bodies."""
    body1 = trimesh.creation.box([10, 10, 10])
    body2 = trimesh.creation.box([10, 10, 10])
    body2.apply_translation([25, 0, 0])
    return {
        0: body1.export(file_type="stl"),
        1: body2.export(file_type="stl"),
    }


@pytest.fixture
def sample_dfm_report_single():
    """Sample DFM report for single body (FDM, SLA, CNC)."""
    return {
        "FDM": {
            "reportType": "FDM",
            "issues": [],
            "thinWallCount": 0,
            "thinWallRegions": [],
            "overhangFaceCount": 0,
            "overhangAreaCm2": 0.0,
            "overhangRegions": [],
            "supportRequired": False,
            "smallDetailCount": 0,
        },
        "SLA": {
            "reportType": "SLA",
            "issues": [],
            "thinWallCount": 0,
            "thinWallRegions": [],
            "resinTrappingRisk": False,
            "resinTrappingRegions": [],
        },
        "CNC_MILL": {
            "reportType": "CNC_MILL",
            "issues": [],
            "sharpCornerCount": 0,
            "sharpCornerRegions": [],
            "internalRadiusIssues": 0,
        },
        "CNC": {
            "reportType": "CNC",
            "issues": [],
            "sharpCornerCount": 0,
        },
    }


@pytest.fixture
def sample_dfm_report_multi():
    """Sample DFM reports for multiple bodies."""
    body1_report = {
        "FDM": {
            "reportType": "FDM",
            "issues": [
                {
                    "category": "thin_wall",
                    "severity": "warning",
                    "title": "Thin Walls (2)",
                    "faceIndices": [1, 2, 3],
                }
            ],
            "thinWallCount": 2,
            "thinWallRegions": [[0, 0, 0], [1, 1, 1]],
            "overhangFaceCount": 0,
            "overhangAreaCm2": 0.0,
            "overhangRegions": [],
            "supportRequired": True,
            "smallDetailCount": 0,
        },
        "CNC_MILL": {
            "reportType": "CNC_MILL",
            "issues": [],
            "sharpCornerCount": 0,
            "sharpCornerRegions": [],
            "internalRadiusIssues": 0,
        },
        "CNC": {
            "reportType": "CNC",
            "issues": [],
            "sharpCornerCount": 0,
        },
    }

    body2_report = {
        "FDM": {
            "reportType": "FDM",
            "issues": [
                {
                    "category": "thin_wall",
                    "severity": "warning",
                    "title": "Thin Walls (3)",
                    "faceIndices": [4, 5, 6, 7],
                }
            ],
            "thinWallCount": 3,
            "thinWallRegions": [[2, 2, 2], [3, 3, 3], [4, 4, 4]],
            "overhangFaceCount": 0,
            "overhangAreaCm2": 0.0,
            "overhangRegions": [],
            "supportRequired": True,
            "smallDetailCount": 1,
        },
        "CNC_MILL": {
            "reportType": "CNC_MILL",
            "issues": [],
            "sharpCornerCount": 5,
            "sharpCornerRegions": [[0, 0, 0], [1, 1, 1]],
            "internalRadiusIssues": 2,
        },
        "CNC": {
            "reportType": "CNC",
            "issues": [],
            "sharpCornerCount": 5,
        },
    }

    return {0: body1_report, 1: body2_report}
