"""Generate small_pin_curved.step — base disc with a tiny cylindrical pin.

A 20 mm Ø × 4 mm tall disc with a 0.6 mm Ø × 4 mm pin standing on top.
The pin's diameter is well below additive process minimums (FDM 3 mm pin,
SLA 0.5 mm pin, SLS 0.8 mm pin).  Cylindrical features confound the
edge-length proxy in :func:`detect_small_features` because the pin is
tessellated into many small triangles around its circumference but its
true feature size (radius) is small — this fixture exercises the
SDF-based small-feature detector that uses inscribed-sphere radius.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "small_pin_curved.step"


def build() -> cq.Workplane:
    base = cq.Workplane("XY").circle(10.0).extrude(4.0)
    pin = (
        cq.Workplane("XY").workplane(offset=4.0).circle(0.3).extrude(4.0)
    )
    return base.union(pin)


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
