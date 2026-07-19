"""
OCC-based B-Rep feature recognition for STEP/IGES files.

Uses cadquery (which wraps OCP/OpenCascade) to traverse the B-Rep topology
and extract manufacturing-relevant features: holes (cylindrical faces),
fillets (concave cylindrical/toroidal faces), pockets (planar face clusters
forming depressions), threads (helicoidal surfaces), undercuts, etc.

All dimensional values are in mm (OCC works in mm by default for STEP).

This module is only called when the uploaded file is STEP or IGES.
Falls back gracefully if cadquery is not installed.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OccFeature:
    """A manufacturing feature detected from B-Rep topology.

    Attributes:
        feature_type:   One of: ``"hole"``, ``"fillet"``, ``"pocket"``,
                        ``"planar_face"``, ``"thread"``, ``"undercut"``,
                        ``"floor_radius"``, ``"text"``, ``"thread_hole"``.
        parameters:     Type-specific parameters:
                        hole    → {"radius_mm", "depth_mm", "axis": [x,y,z]}
                        fillet  → {"radius_mm", "concave": bool}
                        pocket  → {"width_mm", "depth_mm", "floor_radius_mm"}
                        thread  → {"nominal_diameter_mm", "pitch_mm",
                                   "length_mm", "is_external": bool}
                        undercut → {"angle_deg", "width_mm", "depth_mm"}
        face_tag:       OCC face tag (for mapping to tessellation indices).
        centroid:       [x, y, z] in mm.
        normal:         Outward surface normal at centroid (unit vector).
        area_mm2:       Surface area in mm².
    """

    feature_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    face_tag: int = -1
    centroid: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    normal: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0])
    area_mm2: float = 0.0
    # Shortest meaningful dimension of this face (AABB minor axis, mm).
    # Non-zero for planar faces; used by detect_small_features_occ to avoid
    # flagging long-thin faces (e.g. slot walls) as "small" based on sqrt(area).
    bbox_min_mm: float = 0.0


def analyze_step_brep(
    cad_bytes: bytes,
    cad_extension: str = "step",
    process_code: str | None = None,
) -> tuple[list[OccFeature], dict[int, list[int]]]:
    """Load STEP/IGES bytes into CadQuery/OCC and extract B-Rep features.

    Args:
        cad_bytes: STEP/IGES file data as bytes
        cad_extension: File extension ("step", "stp", "igs", "iges")
        process_code: Optional manufacturing process code for adaptive tessellation

    Returns:
        (features, face_tag_to_tri_indices)
        where ``face_tag_to_tri_indices`` maps OCC face tags to lists of
        triangle indices in the tessellated mesh (for overlay GLB generation).

    Falls back to ([], {}) if cadquery is unavailable or parsing fails.
    """
    try:
        import cadquery as cq
        from OCP.BRep import BRep_Tool
        from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
        from OCP.BRepGProp import BRepGProp
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.GeomAbs import (
            GeomAbs_Circle,
            GeomAbs_Cone,
            GeomAbs_Cylinder,
            GeomAbs_Plane,
            GeomAbs_Torus,
        )
        from OCP.gp import gp_Dir, gp_Pnt  # noqa: F401
        from OCP.GProp import GProp_GProps
        from OCP.TopAbs import (  # noqa: F401
            TopAbs_EDGE,
            TopAbs_FACE,
            TopAbs_FORWARD,
            TopAbs_REVERSED,
        )
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS
    except ImportError as _import_err:
        logger.warning(
            "cadquery / OCP not available — skipping OCC B-Rep analysis. "
            "Install cadquery for full DFM analysis on STEP/IGES files. "
            "Import error: %s",
            _import_err,
        )
        return [], {}

    features: list[OccFeature] = []
    face_tag_to_tri: dict[int, list[int]] = {}

    try:
        with tempfile.NamedTemporaryFile(
            suffix=f".{cad_extension}", delete=False
        ) as tmp:
            tmp.write(cad_bytes)
            tmp_path = tmp.name

        # Load into CadQuery
        shape: Any
        if cad_extension in ("step", "stp"):
            shape = cq.importers.importStep(tmp_path)
        else:
            shape = cq.importers.importShape(tmp_path)  # type: ignore[call-arg,arg-type]

        import os

        with contextlib.suppress(OSError):
            os.unlink(tmp_path)  # noqa: PTH108

        if shape is None:
            return [], {}

        occ_shape = shape.val().wrapped
        logger.info(f"OCC: STEP file loaded successfully, size={len(cad_bytes)} bytes")

        # Tessellate for face → triangle mapping
        # OPTIMIZATION: Adaptive tessellation quality based on process type
        from src.core.geometry_optimizations import get_tessellation_tolerance

        file_size_mb = len(cad_bytes) / (1024 * 1024)
        tessellation_tolerance = get_tessellation_tolerance(
            process_code or "DEFAULT", file_size_mb
        )

        logger.info(
            f"OCC: Starting B-Rep tessellation (tolerance={tessellation_tolerance}mm, "
            f"process={process_code or 'DEFAULT'})"
        )
        import time

        start_time = time.time()
        try:
            # Adaptive tessellation: CNC needs high precision, printing can be coarser
            BRepMesh_IncrementalMesh(
                occ_shape,
                tessellation_tolerance,  # Adaptive tolerance based on process
                False,
                0.5,  # Angular deflection (kept constant)
                True,
            )
            elapsed = time.time() - start_time
            logger.info(
                f"OCC: Tessellation completed in {elapsed:.1f}s "
                f"(tolerance={tessellation_tolerance}mm)"
            )

            # If tessellation takes too long (>30s), warn and suggest alternatives
            if elapsed > 30:
                logger.warning(
                    f"OCC: Tessellation slow ({elapsed:.1f}s) - consider "
                    f"disabling B-Rep analysis for this file or using mesh-only DFM"
                )
        except Exception as mesh_err:
            logger.warning(
                f"OCC: Tessellation failed after {time.time() - start_time:.1f}s: {mesh_err}"  # noqa: E501
            )
            # Fall back to empty results rather than crashing
            return [], {}

        explorer = TopExp_Explorer(occ_shape, TopAbs_FACE)
        face_tag = 0
        tri_offset = 0

        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            explorer.Next()
            face_tag += 1

            adaptor = BRepAdaptor_Surface(face)
            surf_type = adaptor.GetType()

            # Get surface area and centroid
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(face, props)
            area_mm2 = float(props.Mass())
            cog = props.CentreOfMass()
            centroid = [float(cog.X()), float(cog.Y()), float(cog.Z())]

            # Determine face orientation for concavity check
            is_reversed = face.Orientation() == TopAbs_REVERSED

            # Extract tessellation for this face and map to triangle indices
            location = face.Location()
            triangulation = BRep_Tool.Triangulation_s(face, location)
            tri_indices: list[int] = []
            if triangulation is not None:
                n_tris = triangulation.NbTriangles()
                tri_indices = list(range(tri_offset, tri_offset + n_tris))
                tri_offset += n_tris

            face_tag_to_tri[face_tag] = tri_indices

            # ── Classify by surface type ────────────────────────────────

            if surf_type == GeomAbs_Cylinder:
                cylinder = adaptor.Cylinder()
                radius_mm = float(cylinder.Radius())
                axis_dir = cylinder.Axis().Direction()
                ax = [float(axis_dir.X()), float(axis_dir.Y()), float(axis_dir.Z())]
                axis_pos = cylinder.Axis().Location()
                axis_origin = [
                    float(axis_pos.X()),
                    float(axis_pos.Y()),
                    float(axis_pos.Z()),
                ]

                # Angular extent of the cylindrical patch: for a cylinder the
                # U parameter is the angle, so LastU-FirstU is the swept arc.
                # A full drilled hole / free-standing pin sweeps ~2π; an edge
                # fillet sweeps a fraction of it.
                try:
                    angular_span_rad = abs(
                        float(adaptor.LastUParameter())
                        - float(adaptor.FirstUParameter())
                    )
                except Exception:
                    angular_span_rad = 0.0

                # Hole: inward-facing cylinder (concave = reversed normal).
                # Convex full cylinders are protruding bosses/pins — calling
                # them "fillet" hid every pin from downstream DFM checks.
                if is_reversed:
                    feat_type = "hole"
                elif angular_span_rad >= 5.0:
                    feat_type = "boss"
                else:
                    feat_type = "fillet"

                # True axial depth: traverse the face's bounding circular edges,
                # project their centres onto the cylinder axis, and take the
                # span.  Falls back to area/(2πr) approximation if no circular
                # edges are found (e.g. partial cylinder bounded by other curves).
                depth_mm: float | None = None
                try:
                    edge_exp = TopExp_Explorer(face, TopAbs_EDGE)
                    projections: list[float] = []
                    while edge_exp.More():
                        edge = TopoDS.Edge_s(edge_exp.Current())
                        edge_exp.Next()
                        try:
                            curve = BRepAdaptor_Curve(edge)
                            if curve.GetType() != GeomAbs_Circle:
                                continue
                            circ = curve.Circle()
                            cloc = circ.Location()
                            offset_x = float(cloc.X()) - axis_origin[0]
                            offset_y = float(cloc.Y()) - axis_origin[1]
                            offset_z = float(cloc.Z()) - axis_origin[2]
                            proj = (
                                offset_x * ax[0]
                                + offset_y * ax[1]
                                + offset_z * ax[2]
                            )
                            projections.append(proj)
                        except Exception:
                            continue
                    if len(projections) >= 2:
                        depth_mm = abs(max(projections) - min(projections))
                except Exception as _depth_err:
                    logger.debug(
                        "edge-based depth failed for face_tag=%d: %s",
                        face_tag,
                        _depth_err,
                    )

                if depth_mm is None and radius_mm > 0:
                    depth_mm = area_mm2 / (2.0 * math.pi * radius_mm)

                params: dict[str, Any] = {
                    "radius_mm": radius_mm,
                    "diameter_mm": radius_mm * 2.0,
                    "axis": ax,
                    "axis_origin": axis_origin,
                    "concave": is_reversed,
                    "angular_span_rad": angular_span_rad,
                }
                if depth_mm is not None:
                    params["depth_mm"] = float(depth_mm)

                features.append(
                    OccFeature(
                        feature_type=feat_type,
                        parameters=params,
                        face_tag=face_tag,
                        centroid=centroid,
                        normal=ax,
                        area_mm2=area_mm2,
                    )
                )

            elif surf_type == GeomAbs_Plane:
                plane = adaptor.Plane()
                norm = plane.Axis().Direction()
                normal = [float(norm.X()), float(norm.Y()), float(norm.Z())]
                if is_reversed:
                    normal = [-n for n in normal]

                # Compute the shortest meaningful dimension of this planar face
                # so detect_small_features_occ can avoid flagging long-thin faces
                # (e.g. 40×0.5 mm slot wall) purely because sqrt(area) looks small.
                planar_bbox_min_mm: float = 0.0
                if triangulation is not None and triangulation.NbNodes() >= 3:
                    nb = triangulation.NbNodes()
                    xs = [triangulation.Node(i).X() for i in range(1, nb + 1)]
                    ys = [triangulation.Node(i).Y() for i in range(1, nb + 1)]
                    zs = [triangulation.Node(i).Z() for i in range(1, nb + 1)]
                    extents = sorted(
                        [
                            max(xs) - min(xs),
                            max(ys) - min(ys),
                            max(zs) - min(zs),
                        ]
                    )
                    # extents[0] ≈ 0 (the face-normal direction for a planar face).
                    # extents[1] is the short side; extents[2] is the long side.
                    planar_bbox_min_mm = (
                        float(extents[1]) if extents[1] > 1e-3 else float(extents[2])
                    )

                features.append(
                    OccFeature(
                        feature_type="planar_face",
                        parameters={
                            "normal": normal,
                            "area_mm2": area_mm2,
                            "is_reversed": is_reversed,
                        },
                        face_tag=face_tag,
                        centroid=centroid,
                        normal=normal,
                        area_mm2=area_mm2,
                        bbox_min_mm=planar_bbox_min_mm,
                    )
                )

            elif surf_type == GeomAbs_Torus:
                torus = adaptor.Torus()
                major_r = float(torus.MajorRadius())
                minor_r = float(torus.MinorRadius())
                features.append(
                    OccFeature(
                        feature_type="fillet",
                        parameters={
                            "radius_mm": minor_r,
                            "major_radius_mm": major_r,
                            "is_torus": True,
                        },
                        face_tag=face_tag,
                        centroid=centroid,
                        area_mm2=area_mm2,
                    )
                )

            elif surf_type == GeomAbs_Cone:
                cone = adaptor.Cone()
                half_angle = math.degrees(float(cone.SemiAngle()))
                features.append(
                    OccFeature(
                        feature_type="cone",
                        parameters={
                            "half_angle_deg": half_angle,
                            "apex_radius_mm": float(cone.RefRadius()),
                        },
                        face_tag=face_tag,
                        centroid=centroid,
                        area_mm2=area_mm2,
                    )
                )

            else:
                # BSpline, BezierSurface, etc. — store as generic
                features.append(
                    OccFeature(
                        feature_type="freeform",
                        parameters={"surface_type": int(surf_type)},
                        face_tag=face_tag,
                        centroid=centroid,
                        area_mm2=area_mm2,
                    )
                )

        # ── Post-process: detect holes with depth, pockets, etc. ──────────
        features = _post_process_features(features, face_tag_to_tri)

    except Exception as exc:
        logger.warning("OCC B-Rep analysis failed: %s", exc)
        return [], {}

    return features, face_tag_to_tri


def _post_process_features(
    features: list[OccFeature],
    face_tag_to_tri: dict[int, list[int]],
) -> list[OccFeature]:
    """Enrich raw surface features with thickness pairings, etc.

    Hole depth is now computed inline in the face loop using circular edges
    projected onto the cylinder axis (exact for full cylinders, falls back to
    area/(2πr) only when no circular edges are present).

    This pass adds:
      * planar wall-thickness — for each pair of planar faces with anti-parallel
        outward normals (dot < -0.95), project their centroid difference onto
        the normal to get the perpendicular gap; record the smallest such gap
        per face in ``parameters['wall_thickness_mm']``.
    """
    _ = face_tag_to_tri  # kept for signature stability; unused here

    planar = [f for f in features if f.feature_type == "planar_face"]
    for feat in planar:
        feat.parameters.setdefault("wall_thickness_mm", None)

    # Compare every pair of planar faces; record the thinnest match.
    n = len(planar)
    for i in range(n):
        a = planar[i]
        an = a.parameters.get("normal", a.normal)
        ac = a.centroid
        for j in range(i + 1, n):
            b = planar[j]
            bn = b.parameters.get("normal", b.normal)
            dot = an[0] * bn[0] + an[1] * bn[1] + an[2] * bn[2]
            if dot >= -0.95:
                continue
            dx = b.centroid[0] - ac[0]
            dy = b.centroid[1] - ac[1]
            dz = b.centroid[2] - ac[2]
            gap = abs(dx * an[0] + dy * an[1] + dz * an[2])
            if gap <= 1e-3:
                continue  # coincident planes, not a wall pair
            for feat in (a, b):
                cur = feat.parameters.get("wall_thickness_mm")
                if cur is None or gap < cur:
                    feat.parameters["wall_thickness_mm"] = float(gap)

    return features


# Deferred import — math may not be imported at module level
import math  # noqa: E402
