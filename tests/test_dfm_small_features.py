"""Small feature detection tests.

Tests for small feature detection in DFM.
Uses emboss-deboss test files.
"""

import io

import pytest
import trimesh

from src.core.geometry import _analyze_single_process

ASSETS_DIR = pytest.importorskip("pathlib").Path(__file__).parent / "assets"


SMALL_FEATURE_FILES = [
    "emboss-deboss-1mm-binary.stl",
]


class TestSmallFeatureGeometry:
    """Test small feature geometry loading."""

    @pytest.mark.parametrize("filename", SMALL_FEATURE_FILES)
    def test_small_feature_loads(self, filename):
        """Test that small feature geometry loads."""
        file_path = ASSETS_DIR / filename

        if not file_path.exists():
            pytest.skip(f"File not found: {filename}")

        bytes_data = file_path.read_bytes()
        stream = io.BytesIO(bytes_data)

        loaded = trimesh.load(stream, file_type="stl")
        assert loaded is not None


class TestSmallFeatureDetection:
    """Test small feature detection in DFM."""

    def test_fdm_small_feature_detection(self):
        """Test FDM small feature detection."""
        file_path = ASSETS_DIR / "emboss-deboss-1mm-binary.stl"

        if not file_path.exists():
            pytest.skip("File not found")

        stl_bytes = file_path.read_bytes()

        result = _analyze_single_process(stl_bytes, "FDM")

        assert result is not None
        assert "reportType" in result or "issues" in result

        if "issues" in result:
            small_feature_issues = [
                i
                for i in result["issues"]
                if i.get("category") in ["small_feature", "detail"]
            ]

            print(f"\nFDM small feature issues: {len(small_feature_issues)}")


class TestMultipleSmallFeatureFiles:
    """Test all small feature test files."""

    @pytest.mark.parametrize(
        "file",
        [pytest.param(f, id=f.stem) for f in sorted(ASSETS_DIR.glob("*emboss*.stl"))],
    )
    def test_small_feature_files(self, file):
        """Test multiple small feature files."""
        stl_bytes = file.read_bytes()

        result = _analyze_single_process(stl_bytes, "FDM")

        assert result is not None
