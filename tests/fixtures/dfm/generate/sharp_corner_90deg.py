"""Generate sharp_corner_90deg.step — block with a 90° internal pocket corner.

A 30 × 30 × 20 mm block with a 12 × 12 × 8 mm rectangular pocket.  The
internal corners of the pocket are exact 90° (zero internal radius), which
CNC milling should flag because every endmill has a non-zero radius — sharp
internal corners are physically impossible without secondary EDM operations.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "sharp_corner_90deg.step"


def build() -> cq.Workplane:
    return (
        cq.Workplane("XY")
        .box(30.0, 30.0, 20.0)
        .faces(">Z")
        .workplane()
        .rect(12.0, 12.0)
        .cutBlind(-8.0)
    )


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
