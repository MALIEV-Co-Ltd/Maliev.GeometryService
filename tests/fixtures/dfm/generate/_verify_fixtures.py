"""Quick sanity check on generated fixtures — print bbox + body counts."""

from __future__ import annotations

import sys
from pathlib import Path

import cadquery as cq

BASE = Path(__file__).resolve().parent.parent


def main() -> None:
    cube = cq.importers.importStep(str(BASE / "cube_25mm.step"))
    bb = cube.val().BoundingBox()
    print(
        f"cube_25mm: bbox=({bb.xlen:.2f}, {bb.ylen:.2f}, {bb.zlen:.2f})", flush=True
    )

    tw = cq.importers.importStep(str(BASE / "thin_wall_1mm.step"))
    bb = tw.val().BoundingBox()
    print(
        f"thin_wall_1mm: bbox=({bb.xlen:.2f}, {bb.ylen:.2f}, {bb.zlen:.2f})",
        flush=True,
    )

    mb = cq.importers.importStep(str(BASE / "multibody_clearance.step"))
    solids = mb.solids().vals()
    print(f"multibody_clearance: {len(solids)} solid(s)", flush=True)
    for i, s in enumerate(solids):
        bb = s.BoundingBox()
        cx = (bb.xmin + bb.xmax) / 2
        print(
            f"  body {i}: extents=({bb.xlen:.2f}, {bb.ylen:.2f}, {bb.zlen:.2f}), "
            f"x_center={cx:.2f}",
            flush=True,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
