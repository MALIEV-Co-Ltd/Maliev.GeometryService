"""Generate undercut_injection.step — base block with a side hole creating an
undercut for straight-pull injection moulding (pull direction = +Z).

A 30 × 20 × 12 mm block with an 8 mm Ø horizontal hole through the +X face.
The hole's interior planar surfaces (entrance + exit) face ±X — both
perpendicular to the +Z pull direction.  More importantly, parts of the hole
are visible from neither the +Z nor the −Z parting plane, so a straight-pull
mould cannot release the part without a side-action core.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "undercut_injection.step"


def build() -> cq.Workplane:
    base = cq.Workplane("XY").box(30.0, 20.0, 12.0)
    # Hole through the part along Y; Y-direction normals create undercut for +Z pull
    return base.faces(">Y").workplane().circle(4.0).cutThruAll()


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
