"""Generate overhang_45deg_rotated.step — a wedge whose overhang only fires
once the build_dir parameter is honoured.

The same wedge geometry as overhang_60deg, but rotated 90° around X so the
"bottom" face now lies along +Y instead of -Z.  The default build direction
(+Z) does not see this as an overhang because the slanted face's normal
projects equally onto +X and +Y, with zero Z component.  When `build_dir`
is set to +Y the detector should flag the same surface as it would in the
canonical orientation.
"""

from __future__ import annotations

import math
from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "overhang_45deg_rotated.step"


def build() -> cq.Workplane:
    # 60° overhang from vertical (= 30° from horizontal): rise = run·tan(30°).
    # Comfortably above the 45° threshold so detection fires unambiguously
    # when the build direction matches the geometry orientation.
    run = 10.0
    rise = run * math.tan(math.radians(30.0))
    height = 30.0
    base = (
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
    # Rotate 90° around X so the original +Z build direction maps to -Y in
    # the rotated frame (cadquery's right-hand rotation around X sends +Y →
    # +Z and +Z → -Y).
    return base.rotate((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), 90.0)


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
