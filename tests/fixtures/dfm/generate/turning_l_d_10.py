"""Generate turning_L_D_10.step — long slender shaft for CNC turning L/D check.

A 5 mm Ø × 50 mm long cylindrical shaft (length/diameter = 10).  CNC turning
suffers chatter and dimensional error when L/D exceeds ~6:1 without a
steady rest, and ≥ 10:1 is firmly in "needs special tooling" territory.
The CNC_TURN analyzer should flag the L/D ratio.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "turning_L_D_10.step"


def build() -> cq.Workplane:
    return cq.Workplane("XY").circle(2.5).extrude(50.0)


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
