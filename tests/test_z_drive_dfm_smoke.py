"""Local smoke tests for representative Z: drive customer assets."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.geometry import _analyze_single_process, _compute_metrics_worker

Z_ROOT = Path("Z:/")
PROCESS_CODES = ("FDM", "SLA", "CNC_MILL", "CNC_TURN")


def _z_asset_cases() -> list[tuple[str, Path]]:
    return [
        ("stl", Z_ROOT / "250 x 150 x 6mm.stl"),
        ("step", Z_ROOT / "draft-2 cover.step"),
        ("stp", Z_ROOT / "THS-PH21E_90X25_2.stp"),
        ("igs", Z_ROOT / "3D CAD_Ornament Max Step_06-5-2026.igs"),
        ("3mf", Z_ROOT / "adapter_v5.3mf" / "adapter_v5.3mf"),
        ("glb", Z_ROOT / "low-poly-fox-compressed.glb"),
    ]


pytestmark = pytest.mark.skipif(
    not Z_ROOT.exists(),
    reason="Z: drive customer asset smoke tests require local Z: mount",
)


@pytest.mark.parametrize(
    ("extension", "asset_path"),
    [
        pytest.param(extension, asset_path, id=extension)
        for extension, asset_path in _z_asset_cases()
    ],
)
def test_z_drive_asset_runs_all_dfm_processes(
    extension: str,
    asset_path: Path,
) -> None:
    if not asset_path.exists():
        pytest.skip(f"Z: asset is not available: {asset_path}")

    source_bytes = asset_path.read_bytes()
    metrics = _compute_metrics_worker(source_bytes, extension)

    assert metrics["triangle_count"] > 0
    assert metrics["volume_cm3"] > 0
    assert metrics.get("mesh_stl_bytes")
    assert metrics.get("cad_glb_bytes")

    cad_extensions = {"step", "stp", "iges", "igs"}
    cad_bytes = source_bytes if extension in cad_extensions else None
    cad_extension = extension if extension in cad_extensions else None

    for process_code in PROCESS_CODES:
        report = _analyze_single_process(
            metrics["mesh_stl_bytes"],
            process_code,
            cad_bytes,
            cad_extension,
        )

        assert "error_type" not in report
        assert report["reportType"] == process_code
        assert isinstance(report["issues"], list)
