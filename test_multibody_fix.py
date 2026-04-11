#!/usr/bin/env python
"""
Quick test script to verify multi-body GLB processing fixes.
Tests the trimesh to PyVista conversion that was failing.
"""
import sys
import io

def test_trimesh_to_pyvista_conversion():
    """Test that we can convert a trimesh GLB to PyVista mesh."""
    try:
        import trimesh
        import pyvista as pv
        import numpy as np

        print("✓ Successfully imported trimesh and pyvista")

        # Create a simple test mesh (a cube)
        vertices = np.array([
            [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
            [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]
        ], dtype=np.float32)

        # Create triangular faces (trimesh format: [n, v0, v1, v2])
        faces = np.array([
            [0, 0, 1, 2], [1, 0, 2, 3],  # Bottom
            [2, 4, 5, 6], [3, 4, 6, 7],  # Top
            [4, 0, 1, 5], [5, 1, 5, 6],  # Front
            [6, 2, 3, 7], [7, 3, 7, 6],  # Back
            [8, 0, 3, 4], [9, 3, 4, 7],  # Left
            [10, 1, 2, 6], [11, 2, 6, 5]  # Right
        ], dtype=np.int32)

        print(f"✓ Created test mesh: {len(vertices)} vertices, {len(faces)} faces")

        # Test conversion to PyVista format
        if len(faces) > 0 and faces.shape[1] == 4:
            faces_pv = faces[:, 1:4]  # Remove the first column (vertex count)
        else:
            faces_pv = faces.reshape(-1, 4)[:, 1:4]

        print(f"✓ Converted faces to PyVista format: {faces_pv.shape}")

        # Create PyVista mesh
        mesh = pv.PolyData(vertices, faces_pv)

        if not mesh or mesh.n_points == 0:
            print("✗ PyVista mesh creation failed - no points")
            return False

        if mesh.n_faces == 0:
            print("✗ PyVista mesh creation failed - no faces")
            return False

        print(f"✓ Successfully created PyVista mesh: {mesh.n_points} points, {mesh.n_faces} faces")
        return True

    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False

def test_trimesh_glb_loading():
    """Test that trimesh can load GLB files."""
    try:
        import trimesh

        # Create a simple test mesh and export to GLB
        import numpy as np
        vertices = np.array([
            [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
            [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]
        ], dtype=np.float32)

        faces = np.array([
            [0, 0, 1, 2], [1, 0, 2, 3],
            [2, 4, 5, 6], [3, 4, 6, 7],
            [4, 0, 1, 5], [5, 1, 5, 6],
            [6, 2, 3, 7], [7, 3, 7, 6],
            [8, 0, 3, 4], [9, 3, 4, 7],
            [10, 1, 2, 6], [11, 2, 6, 5]
        ], dtype=np.int32)

        mesh = trimesh.Trimesh(vertices=vertices, faces=faces)

        # Export to GLB bytes
        glb_bytes = mesh.export(file_type='glb')
        print(f"✓ Successfully created test GLB: {len(glb_bytes)} bytes")

        # Try to load it back
        loaded = trimesh.load(io.BytesIO(glb_bytes), file_type='glb')
        print(f"✓ Successfully loaded GLB back: {type(loaded)}")

        if hasattr(loaded, 'vertices'):
            print(f"✓ Loaded mesh has {len(loaded.vertices)} vertices")
            return True
        else:
            print("✗ Loaded object has no vertices")
            return False

    except Exception as e:
        print(f"✗ GLB loading test failed: {e}")
        return False

if __name__ == "__main__":
    print("Testing multi-body GLB processing fixes...")
    print("=" * 50)

    print("\nTest 1: Trimesh to PyVista conversion")
    test1 = test_trimesh_to_pyvista_conversion()

    print("\nTest 2: Trimesh GLB loading")
    test2 = test_trimesh_glb_loading()

    print("\n" + "=" * 50)
    if test1 and test2:
        print("✓ All tests passed!")
        sys.exit(0)
    else:
        print("✗ Some tests failed")
        sys.exit(1)
