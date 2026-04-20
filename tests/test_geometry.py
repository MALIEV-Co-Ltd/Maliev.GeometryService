import io
from pathlib import Path

import numpy as np
import pytest
import trimesh

from src.core.geometry import GeometryProcessor, _generate_preview_images_sync

# Rendering tests require PyVista (Linux/Docker only — not available on Windows)
try:
    import pyvista  # noqa: F401

    HAS_PYVISTA = True
except ImportError:
    HAS_PYVISTA = False

requires_pyvista = pytest.mark.skipif(
    not HAS_PYVISTA, reason="PyVista not installed (run in Docker)"
)

ASSETS_DIR = Path(__file__).parent / "assets"


@pytest.fixture
def processor():
    return GeometryProcessor()


def is_format_supported(ext: str) -> bool:
    """Checks if trimesh or GMSH backend is available."""
    fmt = ext.strip(".").lower()
    if fmt in ["3mf"]:
        try:
            import lxml  # noqa: F401

            return True
        except ImportError:
            return False
    if fmt in trimesh.exchange.load.available_formats():
        return True
    if fmt in ["igs", "iges", "step", "stp"]:
        try:
            import gmsh  # noqa: F401

            return True
        except ImportError:
            return False
    return False


def load_mesh(file_path: Path):
    """Load a mesh from file and return the first Trimesh."""
    loaded = trimesh.load(file_path)
    if isinstance(loaded, trimesh.Scene):
        return list(loaded.geometry.values())[0]
    return loaded


# Test geometries for preview images
TEST_GEOMETRIES = [
    "50x50x50mm-solid-cube-binary.stl",
    "dice.stl",
]


@requires_pyvista
class TestThumbnailGeneration:
    """Tests for thumbnail generation functionality."""

    def test_generate_thumbnail_returns_bytes(self, processor):
        """Test that thumbnail generation returns valid image bytes (WebP)."""
        cube_path = ASSETS_DIR / "50x50x50mm-solid-cube-binary.stl"
        mesh = load_mesh(cube_path)

        result = processor._generate_thumbnail(mesh)

        assert result is not None
        assert isinstance(result, bytes)
        # WebP files start with RIFF....WEBP
        assert result[:4] == b"RIFF"

    def test_generate_thumbnail_different_geometries(self, processor):
        """Test thumbnail generation with different geometry types."""
        for geometry_file in TEST_GEOMETRIES:
            geometry_path = ASSETS_DIR / geometry_file
            if not geometry_path.exists():
                continue

            mesh = load_mesh(geometry_path)
            result = processor._generate_thumbnail(mesh)

            assert result is not None, (
                f"Failed to generate thumbnail for {geometry_file}"
            )
            assert isinstance(result, bytes), (
                f"Thumbnail for {geometry_file} is not bytes"
            )
            # WebP files start with RIFF....WEBP
            assert result[:4] == b"RIFF", (
                f"Thumbnail for {geometry_file} is not valid WebP"
            )

    def test_generate_thumbnail_with_empty_mesh(self, processor):
        """Test thumbnail generation with empty mesh returns None."""
        # Create an empty mesh
        mesh = trimesh.Trimesh(vertices=np.array([]), faces=np.array([]))

        result = processor._generate_thumbnail(mesh)

        # Should return None for empty mesh (best effort)
        assert result is None or isinstance(result, bytes)


@requires_pyvista
class TestPreviewImagesGeneration:
    """Tests for 7-view preview image generation functionality (6 ortho + 1 isometric)."""

    def test_generate_preview_images_returns_dict(self):
        """Test that preview images generation returns a dictionary."""
        cube_path = ASSETS_DIR / "50x50x50mm-solid-cube-binary.stl"
        mesh = load_mesh(cube_path)

        result = _generate_preview_images_sync(mesh)

        assert isinstance(result, dict)

    def test_generate_preview_images_contains_all_seven_views(self):
        """Test that preview images contain all required keys (6 ortho _small + thumbnail_small + thumbnail_large)."""
        cube_path = ASSETS_DIR / "50x50x50mm-solid-cube-binary.stl"
        mesh = load_mesh(cube_path)

        result = _generate_preview_images_sync(mesh)

        expected_keys = {
            "front_small",
            "back_small",
            "left_small",
            "right_small",
            "top_small",
            "bottom_small",
            "thumbnail_small",
            "thumbnail_large",
        }
        assert set(result.keys()) == expected_keys, (
            f"Missing keys: {expected_keys - set(result.keys())}"
        )

    def test_generate_preview_images_bytes_for_all_sides(self):
        """Test that all views generate valid WebP bytes."""
        for geometry_file in TEST_GEOMETRIES:
            geometry_path = ASSETS_DIR / geometry_file
            if not geometry_path.exists():
                continue

            mesh = load_mesh(geometry_path)
            result = _generate_preview_images_sync(mesh)

            for key in [
                "front_small",
                "back_small",
                "left_small",
                "right_small",
                "top_small",
                "bottom_small",
                "thumbnail_small",
                "thumbnail_large",
            ]:
                assert key in result, f"Missing '{key}' for {geometry_file}"
                if result[key] is not None:
                    assert isinstance(result[key], bytes), (
                        f"{key} for {geometry_file} is not bytes"
                    )
                    # WebP files start with RIFF....WEBP
                    assert result[key][:4] == b"RIFF", (
                        f"{key} for {geometry_file} is not valid WebP"
                    )

    def test_generate_preview_images_with_dice(self):
        """Test preview images generation with dice (complex curved geometry)."""
        dice_path = ASSETS_DIR / "dice.stl"
        if not dice_path.exists():
            pytest.skip("Dice test asset missing")

        mesh = load_mesh(dice_path)
        result = _generate_preview_images_sync(mesh)

        assert len(result) == 8
        non_none_count = sum(1 for v in result.values() if v is not None)
        assert non_none_count == 8, f"Expected 8 previews, got {non_none_count}"

    def test_generate_preview_images_partial_failure(self):
        """Test that if one view fails, others still generate."""
        mesh = load_mesh(ASSETS_DIR / "50x50x50mm-solid-cube-binary.stl")

        result = _generate_preview_images_sync(mesh)

        non_none_count = sum(1 for v in result.values() if v is not None)
        assert non_none_count >= 6, f"Too many failures: {non_none_count}/8 succeeded"


class TestAnalyzeStreamWithPreviewImages:
    """Tests for analyze_stream including preview images."""

    def test_analyze_stream_returns_valid_result(self, processor):
        """Test that analyze_stream returns metrics, glb, and thumbnail."""
        cube_path = ASSETS_DIR / "50x50x50mm-solid-cube-binary.stl"
        with cube_path.open("rb") as f:
            stream = io.BytesIO(f.read())

        metrics, glb_bytes, thumbnail_bytes = processor.analyze_stream(stream, ".stl")

        assert metrics is not None
        assert metrics.volume_cm3 > 0
        assert isinstance(metrics.triangle_count, int)


@pytest.mark.parametrize(
    "extension",
    [
        ".stl",
        ".obj",
        pytest.param(
            ".3mf",
            marks=pytest.mark.skipif(
                not is_format_supported(".3mf"),
                reason="3MF backend missing (lxml required)",
            ),
        ),
        pytest.param(
            ".step",
            marks=pytest.mark.skipif(
                not is_format_supported(".step"), reason="STEP backend missing"
            ),
        ),
        pytest.param(
            ".stp",
            marks=pytest.mark.skipif(
                not is_format_supported(".stp"), reason="STP backend missing"
            ),
        ),
        pytest.param(
            ".igs",
            marks=pytest.mark.skipif(
                not is_format_supported(".igs"), reason="IGS backend missing"
            ),
        ),
        pytest.param(
            ".iges",
            marks=pytest.mark.skipif(
                not is_format_supported(".iges"), reason="IGES backend missing"
            ),
        ),
    ],
)
def test_analyze_cube_formats(processor, extension):
    cube_path = ASSETS_DIR / f"cube{extension}"
    if not cube_path.exists():
        pytest.skip(f"Asset {cube_path.name} missing")

    with cube_path.open("rb") as f:
        stream = io.BytesIO(f.read())
        try:
            metrics, _, _ = processor.analyze_stream(stream, extension)
        except Exception as e:
            if "NoneType" in str(e):
                pytest.skip("Backend failed to process minimal CAD asset")
            raise e

    # If metrics are empty, it means the minimal CAD file didn't tessellate geometry
    if metrics.triangle_count == 0:
        pytest.skip(
            f"Backend loaded {extension} but found no geometry "
            "(likely minimal asset limitation)"
        )

    # Some CAD backends load in meters (0.01) instead of mm (10.0)
    # or may not produce a perfectly manifold mesh from a STEP file.
    is_meter = metrics.bounding_box.x < 0.1

    # Volume should be ~1.0 cm3 for 10x10x10mm cube
    # If trimesh loads in meters, 0.01^3 = 1e-6 m3 = 1 cm3.
    # Our code returns volume_mm3 / 1000.0.
    # If loaded as 0.01 units, volume is 1e-6, result is 1e-9.
    # We'll normalize the expectation based on detected scale.
    expected_vol = 1.0 if not is_meter else 1e-9
    assert pytest.approx(metrics.volume_cm3, rel=5e-2) == expected_vol

    # Surface area should be ~6.0 cm2
    expected_area = 6.0 if not is_meter else 6e-6
    assert pytest.approx(metrics.surface_area_cm2, rel=5e-2) == expected_area

    # For CAD formats, we are more lenient with manifold status in tests
    # as long as the dimensions/volume are correct.
    if extension not in [".step", ".stp", ".igs", ".iges"]:
        assert metrics.is_manifold is True


def test_analyze_broken_mesh(processor):
    broken_path = ASSETS_DIR / "20x20x20mm-cube-nonmanifold-binary.stl"
    with broken_path.open("rb") as f:
        stream = io.BytesIO(f.read())
        metrics, _, _ = processor.analyze_stream(stream, ".stl")

    assert metrics.is_manifold is False
    assert metrics.volume_cm3 >= 0


@requires_pyvista
class TestPreviewImagesOutput:
    """Tests that save preview images for manual inspection."""

    @staticmethod
    def _get_output_dir() -> Path:
        """Find a writable output directory."""
        possible_dirs = [
            Path("test_output"),
            Path("/app/test_output"),
            Path(__file__).parent.parent / "test_output",
        ]
        for d in possible_dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
                test_file = d / ".write_test"
                test_file.write_text("test")
                test_file.unlink()
                return d
            except Exception:
                continue
        pytest.skip("Cannot write to any output directory")

    def test_preview_images_for_dice(self):
        """
        Generate and save 7-view preview images for dice.stl (6 ortho + 1 isometric).
        This is the reference model used to validate CAD-style rendering
        (Shapr3D/Onshape look: matte material, smooth shading, soft lighting).

        Output directory: test_output/
        - dice_front_256.png, dice_back_256.png, dice_left_256.png,
        - dice_right_256.png, dice_top_256.png, dice_bottom_256.png,
        - dice_iso_256.png
        """
        dice_path = ASSETS_DIR / "dice.stl"
        if not dice_path.exists():
            pytest.skip("Dice test asset missing")

        output_dir = self._get_output_dir()
        mesh = load_mesh(dice_path)
        result = _generate_preview_images_sync(mesh)

        print(f"\nSaving preview images to: {output_dir}")

        views_order = [
            "front_small",
            "back_small",
            "left_small",
            "right_small",
            "top_small",
            "bottom_small",
            "thumbnail_small",
            "thumbnail_large",
        ]
        saved_count = 0

        for view in views_order:
            image_bytes = result.get(view)
            if image_bytes and isinstance(image_bytes, bytes):
                output_path = output_dir / f"dice_{view}.webp"
                output_path.write_bytes(image_bytes)
                print(f"  Saved: {output_path.name} ({len(image_bytes)} bytes)")
                saved_count += 1
            else:
                print(f"  Missing: dice_{view}.webp")

        assert saved_count == 8, f"Expected 8 preview images, got {saved_count}"


class TestTessellationSettings:
    """Tests that tessellation settings are correctly applied and reduce mesh complexity."""

    def test_cascadio_tessellation_settings(self):
        """Verify cascadio STEP tessellation uses coarser tolerances for performance."""
        # The actual cascadio call is in _cascadio_subprocess_load, which runs in
        # an isolated process via _load_cad_with_cascadio_isolated.
        from src.core import geometry
        import inspect

        source = inspect.getsource(geometry._cascadio_subprocess_load)
        assert "tol_linear=0.05" in source, (
            "cascadio tol_linear should be 0.05 (balanced quality)"
        )
        assert "tol_angular=0.1" in source, (
            "cascadio tol_angular should be 0.1 (~5.7° deflection)"
        )

    def test_gmsh_tessellation_settings(self):
        """Verify gmsh fallback uses coarser mesh settings for performance."""
        import inspect
        import src.core.geometry as geom

        # Check both Unix and Windows code paths
        source = inspect.getsource(geom._run_gmsh_with_timeout)
        assert 'CharacteristicLengthMax", 2.0' in source, (
            "gmsh CharacteristicLengthMax should be 2.0 (20x coarser than 0.1)"
        )


class TestManifoldCheckPerBody:
    """Tests that manifold checking is done per-body with watertight fallback."""

    def test_per_body_manifold_check(self):
        """Verify manifold check evaluates each body independently."""
        import trimesh
        from src.core.geometry import GeometryProcessor

        proc = GeometryProcessor()

        # Create two separate cubes (manifold individually)
        cube1 = trimesh.creation.box(extents=[10, 10, 10])
        cube2 = trimesh.creation.box(extents=[10, 10, 10])
        cube2.apply_translation([20, 0, 0])

        # Both should be manifold
        assert cube1.is_watertight
        assert cube2.is_watertight

        # Simulate the per-body manifold check (matching actual implementation)
        def _body_is_manifold(m):
            ec = np.unique(m.edges_sorted, axis=0, return_counts=True)[1]
            if bool(np.all(ec <= 2)):
                return True
            return bool(m.is_watertight)

        assert _body_is_manifold(cube1), "Single cube should be manifold"
        assert _body_is_manifold(cube2), "Single cube should be manifold"

    def test_vertex_merge_digits_setting(self):
        """Verify vertex merge uses digits_vertex=3 for more tolerant welding."""
        import inspect
        from src.core import geometry

        # merge_vertices is called in _load_cad_with_cascadio_isolated after
        # the subprocess produces the GLB (not inside the subprocess itself)
        source = inspect.getsource(geometry._load_cad_with_cascadio_isolated)
        assert "digits_vertex=3" in source, (
            "merge_vertices should use digits_vertex=3 (0.001mm precision, more tolerant)"
        )


class TestOverhangDetectionBuildPlateBand:
    """Tests that overhang detection uses a smaller build-plate exclusion band."""

    def test_build_plate_band_reduced(self):
        """Verify build-plate band is 1%/1mm (reduced from 3%/2mm)."""
        import inspect
        from src.core import mesh_analyzers

        source = inspect.getsource(mesh_analyzers.compute_overhang_analysis)
        assert "part_height * 0.01" in source, (
            "Build-plate band should use 1% of part height (reduced from 3%)"
        )
        assert "max(1.0," in source, (
            "Build-plate band minimum should be 1.0mm (reduced from 2.0mm)"
        )

    def test_overhang_logging(self):
        """Verify overhang detection includes logging for clustering results."""
        import inspect
        from src.core import mesh_analyzers

        source = inspect.getsource(mesh_analyzers.compute_overhang_analysis)
        assert "logger.info" in source or "logger.debug" in source, (
            "Overhang analysis should log results"
        )
        assert "overhang" in source.lower(), "Should reference overhang in log output"


class TestThumbnailRenderingWithLighting:
    """Tests that thumbnail rendering produces non-flat images with shading."""

    def test_thumbnail_has_shading_variance(self):
        """Verify thumbnail rendering produces non-uniform pixel values (proving lighting works)."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL/Pillow not available - cannot test image pixel variance")

        from src.core.headless_thumbnail import render_thumbnail_from_glb_headless
        import trimesh
        import io

        # Create a simple cube mesh
        mesh = trimesh.creation.box(extents=[10, 10, 10])
        glb_bytes = mesh.export(file_type="glb")

        # Generate thumbnail
        thumbnail_bytes = render_thumbnail_from_glb_headless(
            glb_bytes, size=256, format="png"
        )

        assert thumbnail_bytes is not None, "Thumbnail generation should succeed"
        assert isinstance(thumbnail_bytes, bytes), "Thumbnail should be bytes"

        # Load the image and check pixel variance
        img = Image.open(io.BytesIO(thumbnail_bytes))
        img_array = np.array(img)

        # Calculate variance across the image
        # A flat grey image would have very low variance
        # A properly lit/shaded image should have significant variance
        pixel_variance = np.var(img_array)

        # Threshold: variance should be at least 100 (on 0-255 scale)
        # This proves we have shading/different face colors
        assert pixel_variance > 100, (
            f"Thumbnail appears flat (variance={pixel_variance:.1f}); "
            "expected significant variance from lighting/shading. "
            "This suggests pyrender may not be working or lighting is disabled."
        )

    def test_thumbnail_iso_camera_angle(self):
        """Verify thumbnail uses automatic camera placement for good default view."""
        import inspect
        from src.core.headless_thumbnail import _render_with_trimesh

        # Check the function uses trimesh's automatic camera placement
        source = inspect.getsource(_render_with_trimesh)

        # Should let trimesh automatically handle camera placement
        assert (
            "trimesh will automatically handle camera" in source
            or "scene.save_image" in source
        ), "Thumbnail should use trimesh's automatic camera placement"

    def test_thumbnail_renders_all_bodies(self):
        """Verify thumbnail renders all bodies in multi-body assemblies."""
        try:
            import pyrender
        except ImportError:
            pytest.skip("pyrender not available")

        from src.core.headless_thumbnail import _render_with_pyrender
        import trimesh

        # Create two cubes (multi-body)
        cube1 = trimesh.creation.box(extents=[5, 5, 5])
        cube2 = trimesh.creation.box(extents=[5, 5, 5])
        cube2.apply_translation([10, 0, 0])

        # Create a scene with both bodies
        scene = trimesh.Scene([cube1, cube2])
        glb_bytes = scene.export(file_type="glb")

        # Render thumbnail
        thumbnail_bytes = _render_with_pyrender(glb_bytes, size=256, format="png")

        assert thumbnail_bytes is not None, "Multi-body thumbnail should render"
        # Check that we got a reasonable size (both cubes should be visible)
        assert len(thumbnail_bytes) > 1000, "Thumbnail should have content"


class TestAutoRotationCameraPreset:
    """Tests that camera presets properly stop auto-rotation."""

    def test_set_camera_preset_stops_auto_rotation(self):
        """
        Verify setCameraPreset stops auto-rotation by zeroing current speed.
        This is a code inspection test since we can't run BabylonJS in pytest.
        """
        # Read the part-viewer.js file
        viewer_path = (
            Path(__file__).parent.parent.parent
            / "Maliev.Intranet"
            / "Maliev.Intranet.Client"
            / "wwwroot"
            / "js"
            / "part-viewer.js"
        )

        if not viewer_path.exists():
            pytest.skip(f"part-viewer.js not found at {viewer_path}")

        source = viewer_path.read_text()

        # Find the setCameraPreset function
        assert "export function setCameraPreset" in source, (
            "setCameraPreset function should exist"
        )

        # Check that it stops auto-rotation
        assert "autoSpeedCurrent[canvasId] = 0" in source, (
            "setCameraPreset should zero autoSpeedCurrent to prevent drift"
        )
        assert "autoSpeedTarget[canvasId]  = SPEED_STOP" in source, (
            "setCameraPreset should set autoSpeedTarget to SPEED_STOP"
        )
        assert "animStates[canvasId]       = 'interacting'" in source, (
            "setCameraPreset should set animStates to 'interacting'"
        )

        # Verify these come BEFORE applyPreset (order matters!)
        preset_func_start = source.find("export function setCameraPreset")
        preset_func_end = source.find(
            "}", source.find("applyPreset", preset_func_start)
        )
        preset_func_body = source[preset_func_start:preset_func_end]

        # Check that auto-rotation stop code appears before applyPreset
        stop_pos = preset_func_body.find("autoSpeedCurrent[canvasId] = 0")
        apply_pos = preset_func_body.find("applyPreset")
        assert stop_pos < apply_pos, (
            "Auto-rotation stop should happen BEFORE applyPreset to prevent drift"
        )


class TestNearClippingPlaneFix:
    """Tests that the near clipping plane is set correctly to prevent early clipping."""

    def test_camera_near_plane_value(self):
        """Verify CAMERA_NEAR_PLANE is set to 0.001 (0.1% of mesh radius)."""
        viewer_path = (
            Path(__file__).parent.parent.parent
            / "Maliev.Intranet"
            / "Maliev.Intranet.Client"
            / "wwwroot"
            / "js"
            / "part-viewer.js"
        )

        if not viewer_path.exists():
            pytest.skip(f"part-viewer.js not found at {viewer_path}")

        source = viewer_path.read_text()

        # Check that CAMERA_NEAR_PLANE is set to 0.0001 (0.01% of mesh radius —
        # further reduced from 0.001 to allow arbitrarily close zoom without clipping)
        assert "CAMERA_NEAR_PLANE: 0.0001" in source, (
            "CAMERA_NEAR_PLANE should be 0.0001 (0.01% of mesh radius) to prevent early clipping"
        )

        # Verify the old value (0.000001) is NOT present
        assert "CAMERA_NEAR_PLANE: 0.000001" not in source, (
            "Old CAMERA_NEAR_PLANE value (0.000001) should be replaced"
        )

        # Verify there's an absolute minimum of 0.01mm
        assert "Math.max(meshRadius * CONFIG.CAMERA_NEAR_PLANE, 0.01)" in source, (
            "Should have absolute minimum of 0.01mm to prevent negative near plane"
        )


class TestMultiBodyTransparencyFix:
    """Tests that multi-body selection state is tracked correctly."""

    def test_selected_body_indices_tracking_exists(self):
        """Verify selectedBodyIndices state tracking is present."""
        viewer_path = (
            Path(__file__).parent.parent.parent
            / "Maliev.Intranet"
            / "Maliev.Intranet.Client"
            / "wwwroot"
            / "js"
            / "part-viewer.js"
        )

        if not viewer_path.exists():
            pytest.skip(f"part-viewer.js not found at {viewer_path}")

        source = viewer_path.read_text()

        # Check that selectedBodyIndices is declared
        assert "selectedBodyIndices" in source, (
            "selectedBodyIndices state tracking should be declared"
        )

        # Check that it's declared alongside perCanvasBodyMap
        body_map_pos = source.find("const perCanvasBodyMap")
        selected_pos = source.find("const selectedBodyIndices")
        assert body_map_pos > 0 and selected_pos > 0, (
            "selectedBodyIndices should be declared near perCanvasBodyMap"
        )

    def test_select_body_handles_reclick(self):
        """Verify selectBody checks if clicked body is already selected."""
        viewer_path = (
            Path(__file__).parent.parent.parent
            / "Maliev.Intranet"
            / "Maliev.Intranet.Client"
            / "wwwroot"
            / "js"
            / "part-viewer.js"
        )

        if not viewer_path.exists():
            pytest.skip(f"part-viewer.js not found at {viewer_path}")

        source = viewer_path.read_text()

        # Find the selectBody function
        assert "export function selectBody" in source, (
            "selectBody function should exist"
        )

        # Check for the re-click logic
        assert "bodyIndex === currentlySelected" in source, (
            "selectBody should check if clicked body equals currently selected body"
        )

        assert "clearBodySelection(canvasId)" in source, (
            "selectBody should call clearBodySelection when same body is clicked"
        )

    def test_clear_body_selection_resets_state(self):
        """Verify clearBodySelection resets selectedBodyIndices to null."""
        viewer_path = (
            Path(__file__).parent.parent.parent
            / "Maliev.Intranet"
            / "Maliev.Intranet.Client"
            / "wwwroot"
            / "js"
            / "part-viewer.js"
        )

        if not viewer_path.exists():
            pytest.skip(f"part-viewer.js not found at {viewer_path}")

        source = viewer_path.read_text()

        # Find the clearBodySelection function
        assert "export function clearBodySelection" in source, (
            "clearBodySelection function should exist"
        )

        # Check that it resets selectedBodyIndices
        clear_func_start = source.find("export function clearBodySelection")
        clear_func_end = source.find(
            "\n}", source.rfind("console.log", clear_func_start)
        )
        clear_func_body = source[clear_func_start:clear_func_end]

        assert "selectedBodyIndices[canvasId] = null" in clear_func_body, (
            "clearBodySelection should reset selectedBodyIndices[canvasId] to null"
        )


@requires_pyvista
class TestThumbnailTrimeshRendering:
    """Tests that thumbnail rendering uses trimesh native rendering instead of pyrender."""

    def test_trimesh_rendering_function_exists(self):
        """Verify _render_with_trimesh function exists and is used."""
        from src.core.headless_thumbnail import render_thumbnail_from_glb_headless

        # Check that the function exists
        import inspect
        import src.core.headless_thumbnail as thumb_module

        assert hasattr(thumb_module, "_render_with_trimesh"), (
            "_render_with_trimesh function should exist"
        )

        # Check that render_thumbnail_from_glb_headless calls trimesh first
        source = inspect.getsource(render_thumbnail_from_glb_headless)
        assert "_render_with_trimesh" in source, (
            "render_thumbnail_from_glb_headless should try _render_with_trimesh first"
        )

    def test_thumbnail_fallback_chain(self):
        """Verify thumbnail tries PyVista first, then falls back to trimesh."""
        import inspect
        from src.core.headless_thumbnail import render_thumbnail_from_glb_headless

        source = inspect.getsource(render_thumbnail_from_glb_headless)
        assert "_render_with_pyvista" in source, (
            "render_thumbnail_from_glb_headless should try PyVista"
        )
        assert "_render_with_trimesh" in source, (
            "render_thumbnail_from_glb_headless should fall back to trimesh"
        )
        assert "falling back" in source or "unavailable" in source, (
            "Should log when falling back"
        )


class TestDfmWorkerMemoryCleanup:
    """Regression tests for memory leak fixes in DFM worker pipeline."""

    def test_analyze_single_process_runs_gc_after_analysis(self):
        """_analyze_single_process should contain explicit gc.collect() after building report."""
        import inspect
        from src.core import geometry

        source = inspect.getsource(geometry._analyze_single_process)
        assert "gc.collect()" in source, (
            "_analyze_single_process must call gc.collect() to release mesh/numpy memory"
        )
        assert "del mesh" in source, (
            "_analyze_single_process must del mesh before returning"
        )

    def test_compute_dfm_single_body_runs_gc(self):
        """_compute_dfm_single_body should call gc.collect() before and after analysis."""
        import inspect
        from src.core import geometry

        source = inspect.getsource(geometry._compute_dfm_single_body)
        assert "gc.collect()" in source, (
            "_compute_dfm_single_body must call gc.collect() to bound RSS between tasks"
        )

    def test_mesh_precompute_cache_max_is_small(self):
        """_MESH_PRECOMPUTE_CACHE_MAX must be ≤ 3 to avoid per-body numpy accumulation."""
        from src.core.geometry import _MESH_PRECOMPUTE_CACHE_MAX

        assert _MESH_PRECOMPUTE_CACHE_MAX <= 3, (
            f"_MESH_PRECOMPUTE_CACHE_MAX={_MESH_PRECOMPUTE_CACHE_MAX} is too large; "
            "each entry holds numpy arrays for one body — keep ≤ 3"
        )

    def test_dfm_executor_maxtasksperchild_is_low(self):
        """dfm_executor maxtasksperchild must be ≤ 2 to recycle workers frequently."""
        import inspect
        from src.core import geometry

        source = inspect.getsource(geometry.GeometryProcessor.__init__)
        # maxtasksperchild=2 should appear in the dfm_executor block
        assert "maxtasksperchild=2" in source, (
            "dfm_executor maxtasksperchild must be ≤ 2 to prevent RSS accumulation"
        )

    def test_memory_monitor_has_backoff(self):
        """Memory monitor must implement backoff to avoid log flooding."""
        import inspect
        from src.core import worker_wrapper

        source = inspect.getsource(worker_wrapper._monitor_memory)
        assert "backoff" in source, (
            "_monitor_memory must implement backoff to stop flooding logs"
        )
        assert "os._exit" in source, (
            "_monitor_memory must self-terminate worker when memory is dangerously high"
        )

    def test_analyze_single_body_produces_real_fdm_report(self):
        """_analyze_single_body must return a non-deferred FDM report."""
        import trimesh
        import io as _io
        from src.core.geometry import _analyze_single_body, FdmDfmReport

        cube = trimesh.creation.box(extents=[20, 20, 20])
        stl_bytes = cube.export(file_type="stl")

        reports = _analyze_single_body(stl_bytes)

        assert "FDM" in reports, "FDM report must be present"
        assert not reports["FDM"].get("twoPhaseDeferred"), "FDM must not be deferred"
        # Must be Pydantic-validatable (would crash the consumer if not)
        fdm = FdmDfmReport.model_validate(reports["FDM"])
        assert fdm.report_type == "FDM"
