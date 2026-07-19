"""Supported file extension DFM contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import trimesh
from trimesh.exchange import gltf

from src.core import geometry
from src.core.geometry import _analyze_single_process, _compute_metrics_worker

ASSETS = Path(__file__).parent / "assets"
PROCESS_CODES = ("FDM", "SLA", "CNC_MILL", "CNC_TURN")


def _generated_glb_bytes() -> bytes:
    mesh = trimesh.load(
        ASSETS / "50x50x50mm-solid-cube-binary.stl",
        file_type="stl",
        force="mesh",
    )
    exported = mesh.export(file_type="glb")
    assert isinstance(exported, bytes)
    return exported


def _generated_gltf_bytes() -> bytes:
    mesh = trimesh.load(
        ASSETS / "50x50x50mm-solid-cube-binary.stl",
        file_type="stl",
        force="mesh",
    )
    exported = gltf.export_gltf(trimesh.Scene(mesh), embed_buffers=True)
    model = exported["model.gltf"]
    assert isinstance(model, bytes)
    return model


def _supported_extension_cases() -> list[tuple[str, bytes]]:
    return [
        ("stl", (ASSETS / "50x50x50mm-solid-cube-binary.stl").read_bytes()),
        ("obj", (ASSETS / "50x50x50mm-solid-cube.obj").read_bytes()),
        ("step", (ASSETS / "50x50x50mm-solid-cube.step").read_bytes()),
        ("stp", (ASSETS / "50x50x50mm-solid-cube.step").read_bytes()),
        ("iges", (ASSETS / "50x50x50mm-solid-cube.iges").read_bytes()),
        ("igs", (ASSETS / "50x50x50mm-solid-cube.iges").read_bytes()),
        ("3mf", (ASSETS / "50x50x50mm-solid-cube.3mf").read_bytes()),
        ("glb", _generated_glb_bytes()),
        ("gltf", _generated_gltf_bytes()),
    ]


@pytest.mark.parametrize(
    ("extension", "source_bytes"),
    [
        pytest.param(extension, source_bytes, id=extension)
        for extension, source_bytes in _supported_extension_cases()
    ],
)
def test_supported_extension_produces_dfm_ready_mesh(
    extension: str,
    source_bytes: bytes,
) -> None:
    metrics = _compute_metrics_worker(source_bytes, extension)

    assert metrics["triangle_count"] > 0
    assert metrics["volume_cm3"] > 0
    assert metrics["surface_area_cm2"] > 0
    assert metrics["bounding_box"]["x"] > 0
    assert metrics["bounding_box"]["y"] > 0
    assert metrics["bounding_box"]["z"] > 0

    stl_bytes = metrics.get("mesh_stl_bytes")
    assert stl_bytes, f"{extension}: metrics worker did not return STL bytes"
    assert metrics.get(
        "cad_glb_bytes"
    ), f"{extension}: metrics worker did not return GLB bytes"

    cad_extensions = {"step", "stp", "iges", "igs"}
    cad_bytes = source_bytes if extension in cad_extensions else None
    cad_extension = extension if extension in cad_extensions else None
    for process_code in PROCESS_CODES:
        report = _analyze_single_process(
            stl_bytes,
            process_code,
            cad_bytes,
            cad_extension,
        )

        assert "error_type" not in report
        assert report["reportType"] == process_code
        assert isinstance(report["issues"], list)


def test_fbx_extension_converts_to_glb_and_produces_dfm_ready_mesh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FBX has no native trimesh loader, so the worker must normalize it to GLB."""

    glb_bytes = _generated_glb_bytes()
    calls: list[bytes] = []

    def convert_fbx(data: bytes) -> bytes:
        calls.append(data)
        return glb_bytes

    monkeypatch.setattr(geometry, "_convert_fbx_to_glb_with_assimp", convert_fbx)

    source_bytes = b"fbx placeholder bytes"
    metrics = geometry._compute_metrics_worker(source_bytes, "fbx")

    assert calls == [source_bytes]
    assert metrics["triangle_count"] > 0
    assert metrics["volume_cm3"] > 0
    assert metrics["surface_area_cm2"] > 0
    assert metrics["body_count"] >= 1
    assert metrics.get("mesh_stl_bytes")
    assert metrics.get("cad_glb_bytes") == glb_bytes

    report = _analyze_single_process(metrics["mesh_stl_bytes"], "FDM")

    assert "error_type" not in report
    assert report["reportType"] == "FDM"
    assert isinstance(report["issues"], list)
