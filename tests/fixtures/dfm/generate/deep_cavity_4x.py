"""Generate deep_cavity_4x.step — 40 mm cube with a deep narrow pocket.

Pocket: 8 mm × 8 mm square, depth 32 mm (depth/width = 4.0). CNC milling
should flag the deep cavity as warning (long stickout reduces tool stability;
typical safe milling ratio is depth/diameter < 4).
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "deep_cavity_4x.step"


def build() -> cq.Workplane:
    return (
        cq.Workplane("XY")
        .box(40.0, 40.0, 40.0)
        .faces(">Z")
        .workplane()
        .rect(8.0, 8.0)
        .cutBlind(-32.0)
    )


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
