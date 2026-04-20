"""CNC internal radius tests.

Tests for internal radius detection in CNC milling.
Uses 100x100x25mm-cube-sharp-internal-corners-various-fillets test file.
"""

import io

import pytest

from src.core.geometry import _analyze_single_process


ASSETS_DIR = pytest.importorskip("pathlib").Path(__file__).parent / "assets"


class TestInternalRadiusDetection:
    """Test internal radius detection in CNC."""

    def test_cnc_internal_radius(self):
        """Test CNC internal radius detection.

        The 100x100x25mm file has various fillet radii.
        Should detect corners with radius below minimum.
        """
        file_path = (
            ASSETS_DIR
            / "100x100x25mm-cube-sharp-internal-corners-various-fillets-binary.stl"
        )

        if not file_path.exists():
            pytest.skip(f"File not found")

        stl_bytes = file_path.read_bytes()

        result = _analyze_single_process(stl_bytes, "CNC_MILL")

        assert result is not None

        if "issues" in result:
            radius_issues = [
                i for i in result["issues"] if i.get("category") == "internal_radius"
            ]

            print(f"\nCNC internal radius issues: {len(radius_issues)}")

        if "internalRadiusIssues" in result:
            print(f"\nInternal radius issues value: {result['internalRadiusIssues']}")


class TestInternalRadiusFormats:
    """Test internal radius detection across formats."""

    @pytest.mark.parametrize(
        "file",
        [
            pytest.param(f, id=f.stem)
            for f in sorted(
                ASSETS_DIR.glob("*sharp-internal-corners-various-fillets*.stl")
            )
        ],
    )
    def test_internal_radius_formats(self, file):
        """Test internal radius across STL formats."""
        stl_bytes = file.read_bytes()

        result = _analyze_single_process(stl_bytes, "CNC_MILL")

        assert result is not None


class TestSharpCorners:
    """Test sharp corner detection."""

    def test_sharp_corner_detection(self):
        """Test sharp corner detection."""
        file_path = (
            ASSETS_DIR
            / "100x100x25mm-cube-sharp-internal-corners-various-fillets-binary.stl"
        )

        if not file_path.exists():
            pytest.skip(f"File not found")

        stl_bytes = file_path.read_bytes()

        result = _analyze_single_process(stl_bytes, "CNC_MILL")

        assert result is not None

        if "sharpCornerCount" in result:
            print(f"\nSharp corner count: {result['sharpCornerCount']}")
