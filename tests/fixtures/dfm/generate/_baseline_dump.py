"""Run the current DFM analysis on every fixture STEP and print the issue counts.

Used to seed the *.expectations.json files. Re-run after algorithm changes to
re-baseline expectations (with code review of any drift).

Usage::

    poetry run python tests/fixtures/dfm/generate/baseline_dump.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Tests run from the GeometryService root; ensure src is importable.
SERVICE_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SERVICE_ROOT))

from src.core.geometry import (  # noqa: E402
    _analyze_single_process,
    _compute_metrics_worker,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent
PROCESSES = ["FDM", "SLA", "SLS", "MJF", "CNC_MILL", "CNC_TURN"]


def analyze_fixture(step_path: Path) -> dict:
    cad_bytes = step_path.read_bytes()
    metrics = _compute_metrics_worker(cad_bytes, "step")
    stl_bytes = metrics.get("mesh_stl_bytes") or b""
    if not stl_bytes:
        return {"error": "no STL produced"}

    out: dict = {
        "body_count": metrics.get("body_count"),
        "triangle_count": metrics.get("triangle_count"),
        "is_manifold": metrics.get("is_manifold"),
        "processes": {},
    }
    for proc in PROCESSES:
        report = _analyze_single_process(stl_bytes, proc, cad_bytes, "step")
        if "error_type" in report:
            out["processes"][proc] = {"error": report["error_type"]}
            continue

        # Group issues by category, capture severity max.
        groups: dict[str, dict] = {}
        for issue in report.get("issues", []):
            cat = issue.get("category", "unknown")
            sev = issue.get("severity", "info")
            entry = groups.setdefault(cat, {"count": 0, "severities": []})
            entry["count"] += 1
            entry["severities"].append(sev)
        out["processes"][proc] = groups
    return out


def main() -> None:
    fixtures = sorted(FIXTURE_DIR.glob("*.step"))
    if not fixtures:
        print(f"no STEP fixtures under {FIXTURE_DIR}", flush=True)
        sys.exit(2)

    summary: dict[str, dict] = {}
    for fx in fixtures:
        print(f"=== {fx.name} ===", flush=True)
        result = analyze_fixture(fx)
        summary[fx.name] = result
        print(json.dumps(result, indent=2, default=str), flush=True)

    out_path = FIXTURE_DIR / "_baseline.json"
    out_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nWrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
