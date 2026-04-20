"""
Headless GLB thumbnail renderer using trimesh's built-in rendering (PyOpenGL/pyglet).
Produces lit, shaded thumbnails with proper depth cues — no flat grey.
Falls back to matplotlib when trimesh rendering is unavailable.
"""

import io
import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

# ── Primary renderer: trimesh native rendering ────────────────────────────────


def _render_with_trimesh(
    glb_bytes: bytes,
    size: int,
    format: str,
) -> bytes | None:
    """Render a lit, shaded thumbnail using trimesh's built-in scene rendering.

    trimesh.save_image() supports PyOpenGL/pyglet backends and provides proper
    Phong shading, directional lights, and smooth normals — much better than
    matplotlib's flat shading.
    """
    # Set environment for headless rendering (OSMesa preferred)
    os.environ.setdefault("PYOPENGL_PLATFORM", "osmesa")

    try:
        import trimesh
    except ImportError:
        logger.debug("trimesh not available")
        return None

    try:
        scene_data = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")

        # Collect all mesh bodies (handles both single-mesh and Scene)
        meshes: list = []
        if isinstance(scene_data, trimesh.Scene):
            for geom in scene_data.geometry.values():
                if isinstance(geom, trimesh.Trimesh) and len(geom.vertices) > 0:
                    meshes.append(geom)
        elif isinstance(scene_data, trimesh.Trimesh) and len(scene_data.vertices) > 0:
            meshes.append(scene_data)

        if not meshes:
            logger.warning("trimesh thumbnail: no valid meshes in GLB")
            return None

        # Use the scene if we have multiple bodies, otherwise create a scene
        if isinstance(scene_data, trimesh.Scene):
            scene = scene_data
        else:
            scene = trimesh.Scene(scene_data)

        # Set up lighting and material for the scene
        # trimesh uses OpenGL lights when rendering

        # Set up the scene for rendering
        # trimesh will automatically handle camera placement for a good view
        # We just need to set the resolution

        # Render to bytes (trimesh returns bytes directly, not a boolean)
        try:
            result = scene.save_image(resolution=(size, size))
        except Exception as e:
            logger.warning("trimesh save_image failed: %s", e)
            return None

        if not isinstance(result, bytes) or len(result) == 0:
            logger.warning("trimesh save_image returned invalid result")
            return None
        logger.info("trimesh thumbnail: %d bytes (%d×%d)", len(result), size, size)
        return result

    except Exception as exc:
        logger.warning("trimesh thumbnail failed: %s", exc)
        return None


# ── Fallback renderer: matplotlib ─────────────────────────────────────────────


def _render_with_pyvista(
    glb_bytes: bytes,
    size: int,
    format: str,
) -> bytes | None:
    """Render thumbnail using PyVista with proper lighting and shading.

    Uses VTK's off-screen rendering which works in headless environments.
    Uses tuned lighting/material settings matching _render_single_view() to avoid
    overly shiny surfaces.
    """
    try:
        import trimesh
        import pyvista as pv
        import numpy as np
        import math

        # Ensure off-screen rendering
        pv.OFF_SCREEN = True

        # Load GLB using trimesh (supports glTF/GLB, including multi-body Scenes)
        tmesh = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")

        # trimesh.load returns a Scene for multi-body GLBs — dump to a single Trimesh
        if isinstance(tmesh, trimesh.Scene):
            # Use the largest mesh by vertex count for thumbnail
            geometries = list(tmesh.geometry.values())
            if not geometries:
                logger.warning("PyVista thumbnail: empty scene")
                return None
            tmesh = max(
                geometries,
                key=lambda g: len(g.vertices) if isinstance(g, trimesh.Trimesh) else 0,
            )

        if not isinstance(tmesh, trimesh.Trimesh) or len(tmesh.vertices) == 0:
            logger.warning("PyVista thumbnail: invalid mesh after load")
            return None

        # Convert to PyVista mesh
        vertices = tmesh.vertices
        faces = tmesh.faces  # shape (N, 3) — trimesh triangles, no count prefix

        # PyVista PolyData requires faces as flat array: [3, v0, v1, v2, 3, v0, v1, v2, ...]
        faces_pv = np.hstack(
            [np.full((len(faces), 1), 3, dtype=np.int32), faces]
        ).flatten()

        # Create PyVista mesh from vertices and faces
        mesh = pv.PolyData(vertices.copy(), faces_pv)
        if not mesh or mesh.n_points == 0:
            return None

        # Calculate bounds for camera positioning (85mm telephoto FOV)
        bounds = mesh.bounds
        x_extent = bounds[1] - bounds[0]
        y_extent = bounds[3] - bounds[2]
        z_extent = bounds[5] - bounds[4]
        max_dim = float(np.max([x_extent, y_extent, z_extent]))

        # 85mm lens equivalent: vertical FOV = 2*atan(24/(2*85)) ≈ 16°
        view_angle = 16.0

        # Compute distance so the model's bounding sphere fills the view with padding
        distance = (max_dim / 2.0) / math.tan(math.radians(view_angle / 2.0)) * 1.3

        # Create renderer with off-screen rendering
        # lighting=None disables default headlight so we can use custom lights
        plotter = pv.Plotter(
            off_screen=True, notebook=False, window_size=[size, size], lighting=None
        )
        plotter.set_background(None)  # Transparent for light/dark mode theme overlay
        plotter.enable_anti_aliasing("msaa")

        # 3-light rig for even illumination (matches _render_single_view)
        plotter.add_light(
            pv.Light(position=(1, -1, 1), intensity=0.35, light_type="scene light")
        )
        plotter.add_light(
            pv.Light(position=(-1, 0.5, 0.5), intensity=0.30, light_type="scene light")
        )
        plotter.add_light(
            pv.Light(position=(0, 0, 1), intensity=0.20, light_type="scene light")
        )

        # Add mesh with proper lighting (tuned to avoid overly shiny surfaces)
        plotter.add_mesh(
            mesh,
            color="#D4D4D4",
            smooth_shading=True,
            specular=0.02,
            specular_power=8,
            ambient=0.55,
            diffuse=0.45,
        )

        # Center the mesh at origin for camera positioning
        mesh_center = mesh.center
        mesh.points -= mesh_center

        # 85mm telephoto camera (matches _render_single_view)
        # Place camera at center + direction * distance, looking back at center
        # Direction is (1/√3, -1/√3, 1/√3) for iso view
        center = np.array([0.0, 0.0, 0.0])
        camera_dir = np.array([1.0, -1.0, 1.0])
        camera_dir = camera_dir / np.linalg.norm(camera_dir)
        cam_pos = center + camera_dir * distance

        plotter.camera_position = cam_pos
        plotter.camera.focal_point = center

        # Set window size
        plotter.window_size = [size, size]

        # Render to image with transparent background
        img = plotter.screenshot(return_img=True, transparent_background=True)
        plotter.close()

        if img is None:
            logger.warning("PyVista screenshot returned None")
            return None

        # Convert to bytes in requested format
        from PIL import Image

        buf = io.BytesIO()
        pil_img = Image.fromarray(img)
        fmt_upper = format.upper()
        # JPEG does not support alpha channel — composite onto white background
        if fmt_upper in ("JPEG", "JPG") and pil_img.mode == "RGBA":
            bg = Image.new("RGB", pil_img.size, (255, 255, 255))
            bg.paste(pil_img, mask=pil_img.split()[3])
            pil_img = bg
        pil_img.save(buf, format=fmt_upper)
        return buf.getvalue()

    except ImportError:
        logger.debug("PyVista not available")
        return None
    except Exception as exc:
        logger.warning(f"PyVista thumbnail failed: {exc}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────


def render_thumbnail_from_glb_headless(
    glb_bytes: bytes,
    size: int = 256,
    format: str = "png",
) -> bytes | None:
    """Render a thumbnail from GLB bytes.

    Tries renderers in order:
    1. PyVista (proper lighting/shading, works headless)
    2. trimesh native (lit/shaded, requires display/OSMesa)
    3. matplotlib (flat shading, last resort)

    Args:
        glb_bytes: GLB file data (Y-up, millimeters — as produced by cascadio)
        size: Thumbnail width/height in pixels
        format: Output format ('png', 'jpeg'/'jpg', or 'webp')

    Returns:
        Thumbnail image bytes or None if all renderers failed
    """
    # Try PyVista first (proper lighting, works in Docker)
    result = _render_with_pyvista(glb_bytes, size, format)
    if result is not None:
        logger.info("Successfully rendered thumbnail using PyVista (with lighting)")
        return result

    # T4d: fallback to trimesh native rendering only — do NOT retry PyVista
    # (the original code called _render_with_pyvista a second time as a dead
    # fallback after the first attempt already failed).
    return _render_with_trimesh(glb_bytes, size, format)


def render_thumbnail_stl_fallback(stl_bytes: bytes, size: int = 256) -> bytes | None:
    """Render a thumbnail from STL bytes (converts STL → GLB internally)."""
    try:
        import trimesh

        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl")
        return render_thumbnail_from_glb_headless(
            mesh.export(file_type="glb"),
            size=size,
        )
    except Exception as exc:
        logger.warning("STL thumbnail failed: %s", exc)
        return None


# ── Simple test ───────────────────────────────────────────────────────────────


def test_headless_rendering():
    """Test the headless thumbnail rendering with a simple cube."""
    print("Testing headless GLB thumbnail rendering...")
    try:
        import trimesh

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
                [0, 1, 2],
                [0, 2, 3],
                [4, 6, 5],
                [4, 7, 6],
                [0, 1, 5],
                [0, 5, 4],
                [2, 3, 7],
                [2, 7, 6],
                [0, 3, 7],
                [0, 7, 4],
                [1, 2, 6],
                [1, 6, 5],
            ],
            dtype=np.int32,
        )
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces)
        glb_bytes = mesh.export(file_type="glb")

        thumbnail = render_thumbnail_from_glb_headless(glb_bytes, size=256)
        if thumbnail:
            with open("test_headless_thumbnail.png", "wb") as f:
                f.write(thumbnail)
            print(f"✓ Rendered {len(thumbnail)} bytes → test_headless_thumbnail.png")
            return True
        print("✗ Failed to render thumbnail")
        return False
    except Exception as exc:
        print(f"✗ Test failed: {exc}")
        return False


if __name__ == "__main__":
    test_headless_rendering()
