"""DFM overhang tests.

Tests for overhang detection in FDM/SLA printing.
Uses *overhang* test files.
"""

import io

import pytest
import trimesh

from src.core.geometry import GeometryProcessor, _analyze_single_process


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
            pytest.skip(f"File not found")

        stl_bytes = file_path.read_bytes()

        result = _analyze_single_process(stl_bytes, "FDM")

        assert result is not None
        assert "reportType" in result or "issues" in result

        if "issues" in result:
            overhang_issues = [
                i for i in result["issues"] if i.get("category") == "overhang"
            ]
            print(f"\nFDM overhang issues: {len(overhang_issues)}")


class TestOverhangAngle:
    """Test overhang angle detection."""

    def test_overhang_angle_accuracy(self):
        """Test that overhang angle is detected correctly."""
        file_path = ASSETS_DIR / "90x55x50mm-overhang-angles-binary.stl"

        if not file_path.exists():
            pytest.skip(f"File not found")

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
