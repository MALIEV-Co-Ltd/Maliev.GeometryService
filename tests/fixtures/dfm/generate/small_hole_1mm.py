"""Generate small_hole_1mm.step — 20 mm cube with three small holes.

Three through-holes of varying diameter (1.0, 1.5, 2.5 mm) drilled through a
20 mm cube along Z.  Used to exercise edge-based hole depth: each hole's
depth_mm should equal 20 mm regardless of diameter (the area/(2πr) heuristic
returned different depths for different diameters).
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "small_hole_1mm.step"


def build() -> cq.Workplane:
    return (
        cq.Workplane("XY")
        .box(20.0, 20.0, 20.0)
        .faces(">Z")
        .workplane()
        .pushPoints([(-6.0, 0.0), (0.0, 0.0), (6.0, 0.0)])
        .circle(0.5)  # 1.0 mm hole
        .cutThruAll()
        .faces(">Z")
        .workplane()
        .pushPoints([(0.0, -6.0)])
        .circle(0.75)  # 1.5 mm hole
        .cutThruAll()
        .faces(">Z")
        .workplane()
        .pushPoints([(0.0, 6.0)])
        .circle(1.25)  # 2.5 mm hole
        .cutThruAll()
    )


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
