"""Generate draft_0deg_casting.step — block with 0° draft, intended for casting.

A simple 30 × 20 × 10 mm rectangular block.  All side walls are perpendicular
to the +Z pull direction (0° draft), which casting and injection moulding
both require to be ≥ 1° (typically 2°).  check_draft_angles_brep should flag
all four side faces.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "draft_0deg_casting.step"


def build() -> cq.Workplane:
    return cq.Workplane("XY").box(30.0, 20.0, 10.0)


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
