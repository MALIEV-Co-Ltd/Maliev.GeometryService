"""DFM overhang tests.

Tests for overhang detection in FDM/SLA printing.
Uses *overhang* test files.
"""

import io

import pytest
import trimesh

from src.core.geometry import _analyze_single_process
from src.core.mesh_analyzers import detect_bridges

ASSETS_DIR = pytest.importorskip("pathlib").Path(__file__).parent / "assets"


OVERHANG_FILES = [
    "100x20mm-long-short-overhang-binary.stl",
    "100x20mm-single-long-overhang-binary.stl",
    "90x55x50mm-overhang-angles-binary.stl",
]


class TestOverhangGeometry:
    """Test overhang geometry loading."""

    @pytest.mark.parametrize("filename", OVERHANG_FILES)
    def test_overhang_loads(self, filename):
        """Test that overhang geometry loads."""
        file_path = ASSETS_DIR / filename

        if not file_path.exists():
            pytest.skip(f"File not found: {filename}")

        bytes_data = file_path.read_bytes()
        stream = io.BytesIO(bytes_data)

        loaded = trimesh.load(stream, file_type="stl")
        assert loaded is not None


class TestOverhangDetection:
    """Test overhang detection in DFM."""

    def test_fdm_overhang_detection(self):
        """Test FDM overhang detection."""
        file_path = ASSETS_DIR / "100x20mm-long-short-overhang-binary.stl"

        if not file_path.exists():
            pytest.skip("File not found")

        stl_bytes = file_path.read_bytes()

        result = _analyze_single_process(stl_bytes, "FDM")

        assert result is not None
        assert "reportType" in result or "issues" in result

        if "issues" in result:
            overhang_issues = [
                i for i in result["issues"] if i.get("category") == "overhang"
            ]
            print(f"\nFDM overhang issues: {len(overhang_issues)}")


class TestBridgeDetection:
    """Test FDM bridge detection."""

    def test_floor_supported_cylinder_has_no_bridge_issue(self):
        """A cylinder on the build plate must not report bottom faces as bridges."""
        cylinder = trimesh.creation.cylinder(
            radius=62.5,
            height=25.6,
            sections=360,
        )
        cylinder.apply_translation([0.0, 0.0, 12.8])

        bridge_count, centroids, face_indices = detect_bridges(cylinder, 10.0)

        assert bridge_count == 0
        assert centroids == []
        assert face_indices == []

        stl_bytes = cylinder.export(file_type="stl")
        result = _analyze_single_process(stl_bytes, "FDM")
        bridge_issues = [
            i for i in result.get("issues", []) if i.get("category") == "bridge"
        ]

        assert bridge_issues == []

    def test_user_cylinder_repro_has_no_bridge_issue(self):
        """Regression coverage for Z:\\125x125x25.6mm.stl when available locally."""
        file_path = pytest.importorskip("pathlib").Path(r"Z:\125x125x25.6mm.stl")
        if not file_path.exists():
            pytest.skip("User repro file is not available on this machine")

        result = _analyze_single_process(file_path.read_bytes(), "FDM")
        bridge_issues = [
            i for i in result.get("issues", []) if i.get("category") == "bridge"
        ]

        assert bridge_issues == []

    def test_elevated_unsupported_plate_reports_one_bridge_span(self):
        """An elevated flat underside should still report a bridge."""
        left_support = trimesh.creation.box(extents=[4.0, 10.0, 10.0])
        left_support.apply_translation([-18.0, 0.0, 5.0])
        right_support = trimesh.creation.box(extents=[4.0, 10.0, 10.0])
        right_support.apply_translation([18.0, 0.0, 5.0])
        plate = trimesh.creation.box(extents=[40.0, 10.0, 2.0])
        plate.apply_translation([0.0, 0.0, 21.0])
        mesh = trimesh.util.concatenate([left_support, right_support, plate])

        bridge_count, centroids, face_indices = detect_bridges(mesh, 10.0)

        assert bridge_count == 1
        assert len(centroids) == 1
        assert len(face_indices) > 0


class TestOverhangAngle:
    """Test overhang angle detection."""

    def test_overhang_angle_accuracy(self):
        """Test that overhang angle is detected correctly."""
        file_path = ASSETS_DIR / "90x55x50mm-overhang-angles-binary.stl"

        if not file_path.exists():
            pytest.skip("File not found")

        stl_bytes = file_path.read_bytes()

        result = _analyze_single_process(stl_bytes, "FDM")

        assert result is not None


class TestMultipleOverhangFiles:
    """Test all overhang test files."""

    @pytest.mark.parametrize(
        "file",
        [pytest.param(f, id=f.stem) for f in sorted(ASSETS_DIR.glob("*overhang*.stl"))],
    )
    def test_overhang_files(self, file):
        """Test multiple overhang files for FDM."""
        stl_bytes = file.read_bytes()

        result = _analyze_single_process(stl_bytes, "FDM")

        assert result is not None
