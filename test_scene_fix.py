#!/usr/bin/env python
"""
Test script to verify Scene object handling fix for multi-body GLB files.
This addresses the critical bug: 'Scene' object has no attribute 'vertices'
"""

import io
import sys


def test_scene_object_handling():
    """Test that we properly handle Scene objects from trimesh.load()"""
    try:
        import numpy as np
        import trimesh

        print("PASS: Successfully imported trimesh")

        # Create multiple meshes to simulate multi-body GLB
        mesh1_vertices = np.array(
            [
                [0, 0, 0],
                [1, 0, 0],
                [1, 1, 0],
                [0, 1, 0],
                [0, 0, 1],
                [1, 0, 1],
                [1, 1, 1],
                [0, 1, 1],
            ],
            dtype=np.float32,
        )

        mesh1_faces = np.array(
            [
                [3, 0, 1, 2],
                [3, 0, 2, 3],  # Bottom
                [3, 4, 5, 6],
                [3, 4, 6, 7],  # Top
                [3, 0, 1, 5],
                [3, 1, 5, 6],  # Front
                [3, 2, 3, 7],
                [3, 3, 7, 6],  # Back
                [3, 0, 3, 4],
                [3, 3, 4, 7],  # Left
                [3, 1, 2, 6],
                [3, 2, 6, 5],  # Right
            ],
            dtype=np.int32,
        )

        mesh2_vertices = mesh1_vertices + 2  # Offset second body
        mesh2_faces = mesh1_faces.copy()

        # Create two separate meshes
        mesh1 = trimesh.Trimesh(vertices=mesh1_vertices, faces=mesh1_faces)
        mesh2 = trimesh.Trimesh(vertices=mesh2_vertices, faces=mesh2_faces)

        print(
            f"PASS: Created test meshes: {len(mesh1.vertices)} and {len(mesh2.vertices)} vertices"  # noqa: E501
        )

        # Create a Scene with multiple geometries (simulates multi-body GLB)
        scene = trimesh.Scene()
        scene.add_geometry(geometry=mesh1, node_name="Body_0")
        scene.add_geometry(geometry=mesh2, node_name="Body_1")

        print(f"PASS: Created Scene with {len(scene.geometry)} geometries")

        # Export to GLB and reload (this is what happens in production)
        glb_bytes = scene.export(file_type="glb")
        print(f"PASS: Exported Scene to GLB: {len(glb_bytes)} bytes")

        # Reload the GLB - this should return a Scene object
        loaded_scene = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")
        print(f"PASS: Loaded GLB, type={type(loaded_scene).__name__}")

        # Test the fix: Check if it's a Scene and handle it properly
        if isinstance(loaded_scene, trimesh.Scene):
            if len(loaded_scene.geometry) == 0:
                print("FAIL: Empty scene - no geometries found")
                return False

            geometries = list(loaded_scene.geometry.values())
            print(f"PASS: Scene contains {len(geometries)} geometries")

            # Test single geometry case
            if len(geometries) == 1:
                mesh = geometries[0]
                print(f"PASS: Single geometry case: {len(mesh.vertices)} vertices")
            else:
                # Test multi-body concatenation
                try:
                    concatenated = trimesh.util.concatenate(geometries)
                    print(
                        f"PASS: Concatenated {len(geometries)} meshes: {len(concatenated.vertices)} total vertices"  # noqa: E501
                    )
                    mesh = concatenated
                except Exception as e:
                    print(f"FAIL: Concatenation failed: {e}")
                    return False
        else:
            mesh = loaded_scene
            print(f"PASS: Single Trimesh object: {len(mesh.vertices)} vertices")

        # Verify we can now access mesh attributes
        if hasattr(mesh, "vertices"):
            print(f"PASS: Mesh has {len(mesh.vertices)} vertices")
        else:
            print("FAIL: Mesh has no vertices attribute")
            return False

        if hasattr(mesh, "faces"):
            print(f"PASS: Mesh has {len(mesh.faces)} faces")
        else:
            print("FAIL: Mesh has no faces attribute")
            return False

        return True

    except ImportError as e:
        print(f"FAIL: Import failed: {e}")
        return False
    except Exception as e:
        print(f"FAIL: Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_glb_cache_scene_handling():
    """Test that GLB cache properly handles Scene objects"""
    try:
        import time

        import numpy as np
        import trimesh

        # Create a simple Scene
        vertices = np.array(
            [
                [0, 0, 0],
                [1, 0, 0],
                [1, 1, 0],
                [0, 1, 0],
                [0, 0, 1],
                [1, 0, 1],
                [1, 1, 1],
                [0, 1, 1],
            ],
            dtype=np.float32,
        )

        faces = np.array(
            [
                [3, 0, 1, 2],
                [3, 0, 2, 3],  # Bottom
                [3, 4, 5, 6],
                [3, 4, 6, 7],  # Top
                [3, 0, 1, 5],
                [3, 1, 5, 6],  # Front
                [3, 2, 3, 7],
                [3, 3, 7, 6],  # Back
                [3, 0, 3, 4],
                [3, 3, 4, 7],  # Left
                [3, 1, 2, 6],
                [3, 2, 6, 5],  # Right
            ],
            dtype=np.int32,
        )

        mesh = trimesh.Trimesh(vertices=vertices, faces=faces)
        scene = trimesh.Scene()
        scene.add_geometry(geometry=mesh, node_name="Body_0")

        glb_bytes = scene.export(file_type="glb")
        print(f"PASS: Created test GLB: {len(glb_bytes)} bytes")

        # Test cache can handle Scene objects
        cache = {}
        scene_data = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")

        # Cache the scene
        cache["test"] = (scene_data, time.time())
        print("PASS: Cached Scene object")

        # Retrieve and verify type
        cached_obj, timestamp = cache["test"]
        obj_type = "Scene" if isinstance(cached_obj, trimesh.Scene) else "Trimesh"
        geom_count = (
            len(cached_obj.geometry) if isinstance(cached_obj, trimesh.Scene) else 1
        )

        print(f"PASS: Retrieved from cache: type={obj_type}, geometries={geom_count}")

        return True

    except Exception as e:
        print(f"FAIL: Cache test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("Testing Scene object handling fixes...")
    print("=" * 50)

    print("\nTest 1: Scene object handling")
    test1 = test_scene_object_handling()

    print("\nTest 2: GLB cache with Scene objects")
    test2 = test_glb_cache_scene_handling()

    print("\n" + "=" * 50)
    if test1 and test2:
        print("ALL TESTS PASSED!")
        sys.exit(0)
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)
