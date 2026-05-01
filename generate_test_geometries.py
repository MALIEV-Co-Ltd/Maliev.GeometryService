#!/usr/bin/env python3
"""Generate sample STL geometry files for testing."""

import os

import numpy as np
import trimesh

# Output directory
output_dir = os.path.join(os.path.dirname(__file__), "tests", "assets")  # noqa: PTH118, PTH120
os.makedirs(output_dir, exist_ok=True)  # noqa: PTH103


def create_pyramid():
    """Create a pyramid (tetrahedron)"""
    # Define vertices for a square pyramid
    vertices = np.array(
        [
            [0, 0, 1],  # apex
            [-1, -1, 0],  # base corner 1
            [1, -1, 0],  # base corner 2
            [1, 1, 0],  # base corner 3
            [-1, 1, 0],  # base corner 4
        ],
        dtype=np.float64,
    )

    # Define faces (triangles) - 4 sides + 2 for base
    faces = np.array(
        [
            [0, 1, 2],  # side 1
            [0, 2, 3],  # side 2
            [0, 3, 4],  # side 3
            [0, 4, 1],  # side 4
            [1, 3, 2],  # base triangle 1
            [1, 4, 3],  # base triangle 2
        ],
        dtype=np.int64,
    )

    return trimesh.Trimesh(vertices=vertices, faces=faces)


def create_helical():
    """Create a twisted/helical shape using a torus knot approximation"""
    # Create a simple twisted torus segment
    mesh = trimesh.creation.torus(
        major_radius=1.0, minor_radius=0.3, major_segments=32, minor_segments=16
    )
    # Apply a twist transformation
    vertices = mesh.vertices.copy()
    # Rotate vertices to create a more interesting shape
    angles = np.arctan2(vertices[:, 1], vertices[:, 0])
    twist_factor = 0.5
    rotated_vertices = np.column_stack(
        [
            vertices[:, 0] * np.cos(angles * twist_factor)
            - vertices[:, 2] * np.sin(angles * twist_factor),
            vertices[:, 1],
            vertices[:, 0] * np.sin(angles * twist_factor)
            + vertices[:, 2] * np.cos(angles * twist_factor),
        ]
    )
    mesh.vertices = rotated_vertices
    return mesh


def create_cylinder():
    """Create a cylinder"""
    return trimesh.creation.cylinder(radius=1.0, height=2.0, sections=32)


def create_sphere():
    """Create a sphere"""
    return trimesh.creation.icosphere(subdivisions=3, radius=1.0)


def create_bracket():
    """Create an L-shaped bracket using a box combination"""
    # Create L-shaped using two boxes
    box1 = trimesh.creation.box(extents=[2, 0.5, 0.5])
    box2 = trimesh.creation.box(extents=[0.5, 1.5, 0.5])

    # Position box2 to create L shape
    box2.vertices += np.array([0, 0.5, 0])

    # Combine meshes
    return trimesh.util.concatenate([box1, box2])


def create_box():
    """Create a simple box (already exists but for completeness)"""
    return trimesh.creation.box(extents=[2, 1, 0.5])


def create_cone():
    """Create a cone"""
    return trimesh.creation.cone(radius=1.0, height=2.0, sections=32)


def main():
    geometries = {
        "pyramid.stl": create_pyramid(),
        "helical.stl": create_helical(),
        "cylinder.stl": create_cylinder(),
        "sphere.stl": create_sphere(),
        "bracket.stl": create_bracket(),
        "cone.stl": create_cone(),
    }

    for filename, mesh in geometries.items():
        filepath = os.path.join(output_dir, filename)  # noqa: PTH118
        mesh.export(filepath)
        print(
            f"Created: {filepath} (vertices: {len(mesh.vertices)}, faces: {len(mesh.faces)})"  # noqa: E501
        )

    print("\nAll sample geometry files created successfully!")


if __name__ == "__main__":
    main()
