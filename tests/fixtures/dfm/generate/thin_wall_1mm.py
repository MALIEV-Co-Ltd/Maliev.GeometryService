"""Generate thin_wall_1mm.step — hollow cylindrical tube with 0.6 mm wall.

Geometry: hollow cylinder, OD 10 mm, ID 8.8 mm (wall thickness 0.6 mm),
height 20 mm, capped on both ends to form a closed solid.  Cylindrical walls
tessellate into many triangles around the circumference, ensuring the
opposing-face pairs accumulate enough faces to pass the cluster-size guard
in compute_thin_wall_analysis.

0.6 mm sits below FDM (0.8), SLS (0.7), and SLA-unsupported (1.0) thresholds,
so all three additive processes should flag at least one thin-wall region.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "thin_wall_1mm.step"


def build() -> cq.Workplane:
    # Outer cylinder OD 10 mm, height 20 mm.  Inner pocket OD 8.8 mm, depth
    # 18 mm leaves 1 mm caps top and bottom and a 0.6 mm cylindrical wall.
    return (
        cq.Workplane("XY")
        .circle(5.0)
        .extrude(20.0)
        .faces(">Z")
        .workplane()
        .circle(4.4)
        .cutBlind(-18.0)
    )


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
