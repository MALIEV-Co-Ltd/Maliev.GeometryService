"""Generate multibody_clearance.step — two separated solids 0.3 mm apart.

Two 10 × 10 × 10 mm cubes, side-by-side along X with a 0.3 mm gap.  The
multi-body STEP exercises the per-body Phase 2 path: both bodies must be
analyzed (regression test for the silent body-drop bug where mesh_stl_bytes
contained only body 0).
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "multibody_clearance.step"


def build() -> cq.Assembly:
    body_a = cq.Workplane("XY").box(10.0, 10.0, 10.0)
    body_b = cq.Workplane("XY").box(10.0, 10.0, 10.0)

    asm = cq.Assembly()
    asm.add(body_a, name="cube_left", loc=cq.Location(cq.Vector(-5.15, 0.0, 0.0)))
    asm.add(body_b, name="cube_right", loc=cq.Location(cq.Vector(5.15, 0.0, 0.0)))
    return asm


def main() -> None:
    asm = build()
    asm.save(str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
