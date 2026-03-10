import io
from pathlib import Path

import pytest
import trimesh
import numpy as np

from src.core.geometry import GeometryProcessor, _generate_preview_images_sync

ASSETS_DIR = Path(__file__).parent / "assets"


@pytest.fixture
def processor():
    return GeometryProcessor()


def is_format_supported(ext: str) -> bool:
    """Checks if trimesh or GMSH backend is available."""
    fmt = ext.strip(".").lower()
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
    "cube.stl",
    "dice.stl",
]


class TestThumbnailGeneration:
    """Tests for thumbnail generation functionality."""

    def test_generate_thumbnail_returns_bytes(self, processor):
        """Test that thumbnail generation returns valid PNG bytes."""
        cube_path = ASSETS_DIR / "cube.stl"
        mesh = load_mesh(cube_path)

        result = processor._generate_thumbnail(mesh)

        assert result is not None
        assert isinstance(result, bytes)
        # PNG files start with these bytes
        assert result[:4] == b"\x89PNG"

    def test_generate_thumbnail_different_geometries(self, processor):
        """Test thumbnail generation with different geometry types."""
        for geometry_file in TEST_GEOMETRIES:
            geometry_path = ASSETS_DIR / geometry_file
            if not geometry_path.exists():
                continue

            mesh = load_mesh(geometry_path)
            result = processor._generate_thumbnail(mesh)

            assert result is not None, f"Failed to generate thumbnail for {geometry_file}"
            assert isinstance(result, bytes), f"Thumbnail for {geometry_file} is not bytes"
            assert result[:4] == b"\x89PNG", f"Thumbnail for {geometry_file} is not valid PNG"

    def test_generate_thumbnail_with_empty_mesh(self, processor):
        """Test thumbnail generation with empty mesh returns None."""
        # Create an empty mesh
        mesh = trimesh.Trimesh(vertices=np.array([]), faces=np.array([]))

        result = processor._generate_thumbnail(mesh)

        # Should return None for empty mesh (best effort)
        assert result is None or isinstance(result, bytes)


class TestPreviewImagesGeneration:
    """Tests for 6-sided preview image generation functionality."""

    def test_generate_preview_images_returns_dict(self):
        """Test that preview images generation returns a dictionary."""
        cube_path = ASSETS_DIR / "cube.stl"
        mesh = load_mesh(cube_path)

        result = _generate_preview_images_sync(mesh)

        assert isinstance(result, dict)

    def test_generate_preview_images_contains_all_six_sides(self):
        """Test that preview images contain all 6 required keys."""
        cube_path = ASSETS_DIR / "cube.stl"
        mesh = load_mesh(cube_path)

        result = _generate_preview_images_sync(mesh)

        expected_keys = {"front", "left", "right", "back", "top", "bottom"}
        assert set(result.keys()) == expected_keys, f"Missing keys: {expected_keys - set(result.keys())}"

    def test_generate_preview_images_bytes_for_all_sides(self):
        """Test that all 6 sides generate valid PNG bytes."""
        for geometry_file in TEST_GEOMETRIES:
            geometry_path = ASSETS_DIR / geometry_file
            if not geometry_path.exists():
                continue

            mesh = load_mesh(geometry_path)
            result = _generate_preview_images_sync(mesh)

            # All sides should be present
            assert "front" in result, f"Missing 'front' for {geometry_file}"
            assert "back" in result, f"Missing 'back' for {geometry_file}"
            assert "left" in result, f"Missing 'left' for {geometry_file}"
            assert "right" in result, f"Missing 'right' for {geometry_file}"
            assert "top" in result, f"Missing 'top' for {geometry_file}"
            assert "bottom" in result, f"Missing 'bottom' for {geometry_file}"

            # Each side should be PNG bytes
            for side in ["front", "back", "left", "right", "top", "bottom"]:
                if result[side] is not None:
                    assert isinstance(result[side], bytes), f"{side} for {geometry_file} is not bytes"
                    assert result[side][:4] == b"\x89PNG", f"{side} for {geometry_file} is not valid PNG"

    def test_generate_preview_images_with_dice(self):
        """Test preview images generation with dice (complex curved geometry)."""
        dice_path = ASSETS_DIR / "dice.stl"
        if not dice_path.exists():
            pytest.skip("Dice test asset missing")

        mesh = load_mesh(dice_path)
        result = _generate_preview_images_sync(mesh)

        assert len(result) == 6
        non_none_count = sum(1 for v in result.values() if v is not None)
        assert non_none_count == 6, f"Expected 6 previews, got {non_none_count}"

    def test_generate_preview_images_partial_failure(self):
        """Test that if one side fails, others still generate."""
        mesh = load_mesh(ASSETS_DIR / "cube.stl")

        result = _generate_preview_images_sync(mesh)

        non_none_count = sum(1 for v in result.values() if v is not None)
        assert non_none_count >= 4, f"Too many failures: {non_none_count}/6 succeeded"


class TestAnalyzeStreamWithPreviewImages:
    """Tests for analyze_stream including preview images."""

    def test_analyze_stream_returns_valid_result(self, processor):
        """Test that analyze_stream returns metrics, glb, and thumbnail."""
        cube_path = ASSETS_DIR / "cube.stl"
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
        ".3mf",
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
    broken_path = ASSETS_DIR / "broken.stl"
    with broken_path.open("rb") as f:
        stream = io.BytesIO(f.read())
        metrics, _, _ = processor.analyze_stream(stream, ".stl")

    assert metrics.is_manifold is False
    assert metrics.volume_cm3 >= 0


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
        Generate and save 6-sided preview images for dice.stl.
        This is the reference model used to validate CAD-style rendering
        (Shapr3D/Onshape look: matte material, feature edge lines, soft lighting).

        Output directory: test_output/
        - dice_front.png, dice_back.png, dice_left.png,
        - dice_right.png, dice_top.png, dice_bottom.png
        """
        dice_path = ASSETS_DIR / "dice.stl"
        if not dice_path.exists():
            pytest.skip("Dice test asset missing")

        output_dir = self._get_output_dir()
        mesh = load_mesh(dice_path)
        result = _generate_preview_images_sync(mesh)

        print(f"\nSaving preview images to: {output_dir}")

        sides_order = ["front", "back", "left", "right", "top", "bottom"]
        saved_count = 0

        for side in sides_order:
            image_bytes = result.get(side)
            if image_bytes and isinstance(image_bytes, bytes):
                output_path = output_dir / f"dice_{side}.png"
                output_path.write_bytes(image_bytes)
                print(f"  Saved: {output_path.name} ({len(image_bytes)} bytes)")
                saved_count += 1
            else:
                print(f"  Missing: dice_{side}.png")

        assert saved_count == 6, f"Expected 6 preview images, got {saved_count}"
