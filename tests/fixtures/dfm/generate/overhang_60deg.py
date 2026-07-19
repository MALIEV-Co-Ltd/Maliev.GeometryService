"""Generate overhang_60deg.step — wedge with a 60°-from-vertical overhang.

Convention: "overhang angle" is measured from the build direction (+Z).  A
60° overhang surface tilts 60° from vertical, which is 30° from horizontal,
so its outward normal has Z component ``−sin(60°) = −0.866`` (well below the
FDM 45° threshold of −0.707, comfortably triggering the overhang detector).

Profile (XZ plane, extruded along Y by 30 mm):

    (0, 30) ------------------- (10, 30)
       |                            |
       |                            |
       |                            | (10, 5.77)
       |                          / |
       |                        /
       |    60° from vertical /
       |                    /
    (0, 0) ___ . __________

Slanted face goes from (0, 0) to (10, 5.77).  Tangent (10, 5.77) has length
≈ 11.547; the right-perpendicular outward normal is (5.77, −10) / 11.547 =
(0.5, −0.866).
"""

from __future__ import annotations

import math
from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "overhang_60deg.step"


def build() -> cq.Workplane:
    run = 10.0
    rise = run * math.tan(math.radians(30.0))  # ≈ 5.77 mm
    height = 30.0
    return (
        cq.Workplane("XZ")
        .polyline([
            (0.0, 0.0),
            (run, rise),
            (run, height),
            (0.0, height),
        ])
        .close()
        .extrude(30.0)
    )


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
