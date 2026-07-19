"""Generate DFM parity baselines for test_dfm_output_parity.py.

Run this ONCE before making any code changes to capture the known-good output.
Re-run ONLY when you intentionally change DFM logic and want to update baselines.

Usage:
    cd Maliev.GeometryService
    python tests/generate_dfm_baselines.py

Writes JSON files to tests/assets/baselines/{stem}__{process_code}.json.
"""

import json
import sys
from pathlib import Path

# Allow imports from src/
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.geometry import _analyze_single_process

ASSETS = Path(__file__).parent / "assets"
BASELINES = Path(__file__).parent / "assets" / "baselines"
BASELINES.mkdir(exist_ok=True)

# (filename, process_code) pairs to capture
CASES: list[tuple[str, str]] = [
    # Solid cube — baseline for all processes
    ("50x50x50mm-solid-cube-binary.stl", "FDM"),
    ("50x50x50mm-solid-cube-binary.stl", "SLA"),
    ("50x50x50mm-solid-cube-binary.stl", "CNC_MILL"),
    # Overhang geometry — FDM/SLA specific
    ("100x20mm-long-short-overhang-binary.stl", "FDM"),
    ("100x20mm-long-short-overhang-binary.stl", "SLA"),
    ("100x20mm-single-long-overhang-binary.stl", "FDM"),
    ("100x20mm-single-long-overhang-binary.stl", "SLA"),
    # Overhang angles
    ("90x55x50mm-overhang-angles-binary.stl", "FDM"),
    ("90x55x50mm-overhang-angles-binary.stl", "SLA"),
    # Sharp corners — CNC specific
    ("100x100x25mm-cube-sharp-internal-corners-various-fillets-binary.stl", "CNC_MILL"),
    ("100x100x25mm-cube-sharp-internal-corners-various-fillets-binary.stl", "FDM"),
    # Deep cavity — for cavity depth ratio testing
    (
        "100x100x100mm-cube-sharp-internal-corners-various-fillets-binary.stl",
        "CNC_MILL",
    ),
    ("100x100x100mm-cube-sharp-internal-corners-various-fillets-binary.stl", "FDM"),
    ("100x100x100mm-cube-sharp-internal-corners-various-fillets-binary.stl", "SLA"),
    # Small features
    ("emboss-deboss-1mm-binary.stl", "FDM"),
    ("emboss-deboss-1mm-binary.stl", "SLA"),
    # Holes
    ("70x40x30mm-cube-variuos-holes-binary.stl", "CNC_MILL"),
    ("70x40x30mm-cube-variuos-holes-binary.stl", "FDM"),
    # Multi-body
    ("50mm-polygon-multibodies-nonoverlap-binary.stl", "FDM"),
    ("50mm-polygon-multibodies-overlap-binary.stl", "FDM"),
    # Sphere - curved surface
    ("50mm-sphere-binary.stl", "FDM"),
    ("50mm-sphere-binary.stl", "SLA"),
    # Existing test files
    ("dice.stl", "FDM"),
    ("dice.stl", "SLA"),
]


def normalize(report: dict) -> dict:
    """Normalize a DFM report for stable comparison.

    - Sorts all region lists (list-of-lists) to remove ordering sensitivity.
    - Rounds float values to 4 decimal places.
    - Removes non-deterministic fields (e.g. timing).
    """

    def _sort_regions(v: object) -> object:
        if isinstance(v, list):
            if v and isinstance(v[0], list):
                # list of [x, y, z] — sort each inner list then sort outer
                return sorted([sorted(inner) for inner in v])
            return [_sort_regions(i) for i in v]
        if isinstance(v, float):
            return round(v, 4)
        if isinstance(v, dict):
            return {k: _sort_regions(vv) for k, vv in v.items()}
        return v

    skip_keys = {"twoPhaseDeferred"}
    return {k: _sort_regions(v) for k, v in report.items() if k not in skip_keys}


def main() -> None:
    errors: list[str] = []
    for filename, process_code in CASES:
        path = ASSETS / filename
        if not path.exists():
            print(f"  SKIP {filename} — file not found")
            continue

        stl_bytes = path.read_bytes()
        print(f"  Analyzing {filename} [{process_code}] ...", end=" ", flush=True)
        result = _analyze_single_process(stl_bytes, process_code)

        if "error_type" in result:
            msg = f"ERROR {filename} [{process_code}]: {result}"
            print(msg)
            errors.append(msg)
            continue

        normalized = normalize(result)
        stem = path.stem
        out_path = BASELINES / f"{stem}__{process_code}.json"
        out_path.write_text(json.dumps(normalized, indent=2, sort_keys=True))
        count_keys = [k for k in result if k.endswith("Count")]
        counts = {k: result[k] for k in count_keys}
        print(f"OK -> {out_path.name}  {counts}")

    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print(f"\nAll baselines written to {BASELINES}")


if __name__ == "__main__":
    main()
