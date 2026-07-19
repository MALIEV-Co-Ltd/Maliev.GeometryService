"""B-rep-driven DFM checks for casting, sheet-metal, injection-moulding, and
threaded-feature constraints.

These analyzers operate on the ``OccFeature`` catalog produced by
:mod:`src.core.occ_analyzer` rather than the tessellated mesh.  Working from
B-rep topology gives exact face dimensions (planar normals, cylinder radii,
cone half-angles) without tessellation noise — the same accuracy benefit the
existing :func:`detect_small_features_occ` provides for additive small-feature
detection.

The functions here are *pure* — they take the feature list and a thresholds
dict, and return a list of issue dicts in the same shape that the printing
and CNC analyzers produce (``category``, ``severity``, ``title``,
``description``, ``faceIndices``).  They do not mutate ``occ_features``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pull-direction inference
# ---------------------------------------------------------------------------


def infer_pull_direction(occ_features: Iterable[Any]) -> tuple[float, float, float]:
    """Pick a casting/moulding pull direction from the planar face area majority.

    Heuristic: the pull direction is the axis along which the most planar-face
    area is normal-aligned (i.e. faces that are perpendicular to the pull
    direction lock the part in the mould; faces parallel to the pull direction
    can release).  We score each candidate axis (±X, ±Y, ±Z) by total area
    of planar faces whose normal is anti-parallel to that axis, then return
    the axis whose total is largest — that's the direction the part is pulled
    away from those locked faces.

    Returns a unit vector (defaults to +Z if no signal).
    """
    candidates: list[tuple[float, float, float]] = [
        (1.0, 0.0, 0.0),
        (-1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, -1.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, -1.0),
    ]
    best_axis = (0.0, 0.0, 1.0)
    best_score = -1.0
    for axis in candidates:
        score = 0.0
        for f in occ_features:
            if getattr(f, "feature_type", None) != "planar_face":
                continue
            n = f.parameters.get("normal", f.normal)
            dot = n[0] * axis[0] + n[1] * axis[1] + n[2] * axis[2]
            # Faces whose normals point along +axis are the ones the mould
            # bottom (= pulling backward along axis) sees.
            if dot > 0.95:
                score += float(getattr(f, "area_mm2", 0.0))
        if score > best_score:
            best_score = score
            best_axis = axis
    return best_axis


# ---------------------------------------------------------------------------
# Draft angle check (casting / injection moulding)
# ---------------------------------------------------------------------------


def check_draft_angles_brep(
    occ_features: Iterable[Any],
    min_draft_deg: float,
    pull_dir: tuple[float, float, float] | None = None,
) -> list[dict[str, Any]]:
    """Flag planar SIDE-WALL faces with insufficient draft for casting/moulding.

    Draft is the tilt of a face away from the pull direction.  The "side walls"
    of a moulded part — faces whose normals are roughly perpendicular to the
    pull direction — need a positive draft angle so the part can release.
    Mathematically: draft = 90° − angle_between(normal, pull_dir), so faces
    with ``|dot(normal, pull)| < sin(min_draft_deg)`` have draft below the
    threshold and are flagged.  Top/bottom faces (parallel to the pull plane,
    |dot| ≈ 1) are never flagged because their draft is 90°.

    Returns a list of issue dicts; empty if everything is OK.
    """
    feats = list(occ_features)
    if pull_dir is None:
        pull_dir = infer_pull_direction(feats)

    px, py, pz = pull_dir
    pull_norm = math.sqrt(px * px + py * py + pz * pz)
    if pull_norm < 1e-9:
        return []
    px, py, pz = px / pull_norm, py / pull_norm, pz / pull_norm

    threshold_dot = math.sin(math.radians(min_draft_deg))
    offending: list[tuple[Any, float]] = []
    for f in feats:
        if getattr(f, "feature_type", None) != "planar_face":
            continue
        n = f.parameters.get("normal", f.normal)
        dot = abs(n[0] * px + n[1] * py + n[2] * pz)
        # Side walls have |dot| close to 0 (normal perpendicular to pull) and
        # need draft.  Faces parallel to the pull plane (|dot| close to 1)
        # don't need draft.
        if dot < threshold_dot:
            draft_deg = math.degrees(math.asin(min(1.0, dot)))
            offending.append((f, draft_deg))

    if not offending:
        return []

    face_indices_all: list[int] = []
    min_draft_seen = 90.0
    for feat, draft in offending:
        face_indices_all.append(int(getattr(feat, "face_tag", -1)))
        min_draft_seen = min(min_draft_seen, draft)

    return [
        {
            "category": "draft_angle",
            "severity": "warning",
            "title": f"Insufficient draft ({len(offending)} face(s))",
            "description": (
                f"{len(offending)} planar face(s) have draft below "
                f"{min_draft_deg:.1f}° (worst: {min_draft_seen:.1f}°). "
                f"Casting / injection moulding requires positive draft to "
                f"release the part from the tool."
            ),
            "value": float(len(offending)),
            "threshold": float(min_draft_deg),
            "faceIndices": face_indices_all,
            "metadata": {
                "pull_direction": [px, py, pz],
                "min_draft_deg_observed": min_draft_seen,
            },
        }
    ]


# ---------------------------------------------------------------------------
# Threaded-hole engagement check
# ---------------------------------------------------------------------------


def check_thread_engagement_brep(
    occ_features: Iterable[Any],
    min_engagement_ratio: float = 1.5,
) -> list[dict[str, Any]]:
    """Flag threaded holes whose length / diameter ratio is below the minimum.

    Standard machine-screw practice: tapped holes need ≥ 1·D engagement in
    steel and ≥ 1.5·D in aluminium for full thread strength.  ``occ_features``
    of type ``"thread"`` carry ``length_mm`` and ``nominal_diameter_mm``; we
    flag any whose ratio is below ``min_engagement_ratio``.
    """
    feats = list(occ_features)
    offending: list[tuple[Any, float]] = []
    for f in feats:
        if getattr(f, "feature_type", None) != "thread":
            continue
        params = getattr(f, "parameters", {})
        length = params.get("length_mm")
        nominal = params.get("nominal_diameter_mm")
        if length is None or nominal is None or nominal <= 0:
            continue
        ratio = float(length) / float(nominal)
        if ratio < min_engagement_ratio:
            offending.append((f, ratio))

    if not offending:
        return []

    return [
        {
            "category": "thread_engagement",
            "severity": "warning",
            "title": f"Shallow tapped hole(s) ({len(offending)})",
            "description": (
                f"{len(offending)} threaded hole(s) have length/diameter "
                f"below {min_engagement_ratio}; typical practice is "
                f"≥ 1·D in steel, ≥ 1.5·D in aluminium."
            ),
            "value": float(len(offending)),
            "threshold": float(min_engagement_ratio),
            "faceIndices": [int(getattr(f, "face_tag", -1)) for f, _ in offending],
        }
    ]


# ---------------------------------------------------------------------------
# Undercut detection
# ---------------------------------------------------------------------------


def check_undercuts_brep(
    occ_features: Iterable[Any],
    pull_dir: tuple[float, float, float] | None = None,
) -> list[dict[str, Any]]:
    """Flag planar faces whose outward normal points opposite the pull direction.

    A face whose normal projects negatively onto the pull direction is on
    the "back" of the part and creates an undercut: pulling the part along
    +pull_dir would require deforming the tool past that face.  This is a
    coarse first-order undercut detector — true undercut analysis needs
    visibility checks against the parting plane, but flagging back-facing
    planar surfaces catches the obvious cases (slots, side holes) that block
    straight-pull moulds.
    """
    feats = list(occ_features)
    if pull_dir is None:
        pull_dir = infer_pull_direction(feats)

    px, py, pz = pull_dir
    pull_norm = math.sqrt(px * px + py * py + pz * pz)
    if pull_norm < 1e-9:
        return []
    px, py, pz = px / pull_norm, py / pull_norm, pz / pull_norm

    offending: list[Any] = []
    for f in feats:
        if getattr(f, "feature_type", None) != "planar_face":
            continue
        n = f.parameters.get("normal", f.normal)
        dot = n[0] * px + n[1] * py + n[2] * pz
        # Negative dot = face normal opposite pull direction = back-of-part.
        # Use −0.3 so faces tilted more than ~70° from pull are not yet flagged.
        if dot < -0.3:
            offending.append(f)

    if not offending:
        return []

    return [
        {
            "category": "undercut",
            "severity": "warning",
            "title": f"Possible undercut(s) ({len(offending)})",
            "description": (
                f"{len(offending)} face(s) point opposite the inferred pull "
                f"direction ({px:+.2f},{py:+.2f},{pz:+.2f}); these block a "
                "single-cavity straight-pull mould and may need a side-action."
            ),
            "value": float(len(offending)),
            "faceIndices": [int(getattr(f, "face_tag", -1)) for f in offending],
            "metadata": {"pull_direction": [px, py, pz]},
        }
    ]
