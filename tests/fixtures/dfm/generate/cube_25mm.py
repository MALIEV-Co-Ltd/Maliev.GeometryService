"""Generate cube_25mm.step — clean 25 mm baseline cube.

A simple solid cube with no thin walls, no overhangs (when printed flat),
no small features. The clean baseline: any process should report zero issues
(or only structural/orientation hints, no severity).
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

OUTPUT = Path(__file__).resolve().parent.parent / "cube_25mm.step"


def build() -> cq.Workplane:
    return cq.Workplane("XY").box(25.0, 25.0, 25.0)


def main() -> None:
    cq.exporters.export(build(), str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
