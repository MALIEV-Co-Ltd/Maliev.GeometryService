"""Performance smoke test for the DFM pipeline.

Asserts a generous wall-clock budget for Phase 1 + Phase 2 (FDM) on the
small fixture set.  CI runners and developer machines vary so the budget is
deliberately loose — its job is to catch *order-of-magnitude* regressions,
not micro-optimization drift.

Override the budget per-machine by setting ``DFM_PERF_BUDGET_S`` in the
environment.  Skipped if cadquery / OCP are unavailable.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dfm"
DEFAULT_BUDGET_S = float(os.environ.get("DFM_PERF_BUDGET_S", "20.0"))

cadquery = pytest.importorskip("cadquery")  # noqa: F841 — gates the test


def _phase1_then_phase2(step_path: Path, process_code: str = "FDM") -> float:
    from src.core.geometry import _analyze_single_process, _compute_metrics_worker

    cad_bytes = step_path.read_bytes()

    start = time.perf_counter()
    metrics = _compute_metrics_worker(cad_bytes, "step")
    stl_bytes = metrics.get("mesh_stl_bytes") or b""
    assert stl_bytes, f"{step_path.name}: tessellation produced no STL"

    report = _analyze_single_process(stl_bytes, process_code, cad_bytes, "step")
    elapsed = time.perf_counter() - start
    assert (
        "error_type" not in report
    ), f"{step_path.name}/{process_code} errored: {report}"
    return elapsed


@pytest.mark.parametrize(
    "fixture_name",
    [
        "cube_25mm.step",
        "thin_wall_1mm.step",
        "small_hole_1mm.step",
        "overhang_60deg.step",
    ],
)
def test_dfm_pipeline_within_budget(fixture_name: str) -> None:
    step_path = FIXTURE_DIR / fixture_name
    if not step_path.exists():
        pytest.skip(f"fixture {fixture_name} not generated")

    elapsed = _phase1_then_phase2(step_path, "FDM")
    assert elapsed < DEFAULT_BUDGET_S, (
        f"{fixture_name}: phase1+phase2 took {elapsed:.2f}s "
        f"(budget {DEFAULT_BUDGET_S}s) — possible perf regression"
    )
