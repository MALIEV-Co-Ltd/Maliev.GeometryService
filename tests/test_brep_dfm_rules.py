"""Direct tests for the B-rep DFM rules added in Stage 5.

Verifies draft-angle, undercut, and thread-engagement detection on the
``OccFeature`` catalog produced by analyze_step_brep, using the
draft_0deg_casting and undercut_injection STEP fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

cadquery = pytest.importorskip("cadquery")  # noqa: F841

from src.core.brep_dfm_rules import (  # noqa: E402
    check_draft_angles_brep,
    check_thread_engagement_brep,
    check_undercuts_brep,
    infer_pull_direction,
)
from src.core.occ_analyzer import analyze_step_brep  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dfm"


def _load_features(name: str) -> list:
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"fixture {name} not generated")
    features, _ = analyze_step_brep(path.read_bytes(), "step")
    return features


def test_draft_zero_block_flags_all_side_walls() -> None:
    features = _load_features("draft_0deg_casting.step")
    assert features, "OCC analysis must produce features for draft fixture"
    issues = check_draft_angles_brep(features, min_draft_deg=1.0)
    assert issues, "draft check must flag the 0° block"
    iss = issues[0]
    assert iss["category"] == "draft_angle"
    # 4 side walls have 0° draft. Top + bottom have 90° draft (parallel pull
    # direction) — they should NOT be flagged.
    assert int(iss["value"]) >= 4


def test_undercut_block_flags_back_face() -> None:
    features = _load_features("undercut_injection.step")
    assert features, "OCC analysis must produce features for undercut fixture"
    # Force pull_dir = +Z so the side hole's −Y entrance face becomes a
    # straight-pull undercut.
    issues = check_undercuts_brep(features, pull_dir=(0.0, 0.0, 1.0))
    # The top/bottom faces are flagged separately by the planar pair logic;
    # the −Y entrance to the hole is the canonical undercut for +Z pull.
    assert any(i.get("category") == "undercut" for i in issues), issues


def test_pull_direction_for_axis_aligned_block_is_one_axis() -> None:
    features = _load_features("draft_0deg_casting.step")
    pull = infer_pull_direction(features)
    assert pull in {
        (1.0, 0.0, 0.0),
        (-1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, -1.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, -1.0),
    }


def test_thread_engagement_passes_when_no_threads() -> None:
    features = _load_features("draft_0deg_casting.step")
    issues = check_thread_engagement_brep(features)
    assert issues == [], "no threaded features → no warnings"


def test_thread_engagement_flags_shallow_thread() -> None:
    # Hand-craft a synthetic thread feature without a fixture.
    from src.core.occ_analyzer import OccFeature

    feats = [
        OccFeature(
            feature_type="thread",
            parameters={"length_mm": 1.5, "nominal_diameter_mm": 6.0},
            face_tag=99,
        ),
    ]
    issues = check_thread_engagement_brep(feats, min_engagement_ratio=1.5)
    assert len(issues) == 1
    assert issues[0]["category"] == "thread_engagement"
