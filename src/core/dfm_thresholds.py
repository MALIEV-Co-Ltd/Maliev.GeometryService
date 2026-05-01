"""
DFM design rule thresholds for all supported manufacturing processes.

Sources:
- 3D printing rules: industry-standard design guidelines reference table
  (FDM, SLA, SLS, MJF, Material Jetting, Binder Jetting, DMLS)
- CNC milling rules: standard machining design guidelines
- CNC turning rules: standard turning design guidelines
- Tool diameter table: standard endmill tooling reference
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 3D Printing Design Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrintingDesignRules:
    """Design rules for additive manufacturing processes.

    All dimensional values are in mm.
    ``None`` means the rule is not applicable for this process
    (e.g. unsupported_wall_mm=None for powder-bed processes that need no
    support structures).
    """

    # Minimum wall thickness connected on ≥2 sides
    supported_wall_mm: float | None
    # Minimum wall thickness connected on <2 sides (free-standing)
    unsupported_wall_mm: float | None
    # Maximum overhang angle from vertical before support is required.
    # None means support is always required regardless of angle.
    max_overhang_deg: float | None
    # Minimum width of embossed/engraved details
    embossed_width_mm: float
    # Minimum height of embossed/engraved details
    embossed_height_mm: float
    # Maximum horizontal unsupported bridge span. None = not applicable.
    bridge_span_mm: float | None
    # Minimum hole diameter that can be reliably reproduced
    min_hole_diameter_mm: float
    # Minimum clearance between connecting/moving parts. None = not applicable.
    connecting_clearance_mm: float | None
    # Minimum diameter of escape/drainage holes. None = not applicable.
    escape_hole_diameter_mm: float | None
    # Minimum size for small features (pins, protrusions, thin details)
    min_feature_mm: float
    # Minimum printable pin diameter
    pin_diameter_mm: float
    # Expected dimensional accuracy as percentage of nominal dimension
    tolerance_percent: float
    # Absolute lower tolerance limit in mm (applies below a certain size)
    tolerance_lower_mm: float


PRINTING_RULES: dict[str, PrintingDesignRules] = {
    # ── Fused Deposition Modeling ──────────────────────────────────────────
    "FDM": PrintingDesignRules(
        supported_wall_mm=0.8,
        unsupported_wall_mm=0.8,
        max_overhang_deg=45.0,
        embossed_width_mm=0.6,
        embossed_height_mm=2.0,
        bridge_span_mm=10.0,
        min_hole_diameter_mm=2.0,
        connecting_clearance_mm=0.5,
        escape_hole_diameter_mm=None,
        min_feature_mm=2.0,
        pin_diameter_mm=3.0,
        tolerance_percent=0.5,
        tolerance_lower_mm=0.5,
    ),
    # ── Stereolithography / DLP ────────────────────────────────────────────
    "SLA": PrintingDesignRules(
        supported_wall_mm=0.5,
        unsupported_wall_mm=1.0,
        max_overhang_deg=None,  # support always required
        embossed_width_mm=0.4,
        embossed_height_mm=0.4,
        bridge_span_mm=None,
        min_hole_diameter_mm=0.5,
        connecting_clearance_mm=0.5,
        escape_hole_diameter_mm=4.0,
        min_feature_mm=0.2,
        pin_diameter_mm=0.5,
        tolerance_percent=0.5,
        tolerance_lower_mm=0.15,
    ),
    # Alias used in existing process code
    "SLA_DLP": PrintingDesignRules(
        supported_wall_mm=0.5,
        unsupported_wall_mm=1.0,
        max_overhang_deg=None,
        embossed_width_mm=0.4,
        embossed_height_mm=0.4,
        bridge_span_mm=None,
        min_hole_diameter_mm=0.5,
        connecting_clearance_mm=0.5,
        escape_hole_diameter_mm=4.0,
        min_feature_mm=0.2,
        pin_diameter_mm=0.5,
        tolerance_percent=0.5,
        tolerance_lower_mm=0.15,
    ),
    # ── Selective Laser Sintering (powder bed — no supports needed) ────────
    "SLS": PrintingDesignRules(
        supported_wall_mm=0.7,
        unsupported_wall_mm=None,  # powder bed self-supports
        max_overhang_deg=None,  # no overhangs/supports concern
        embossed_width_mm=1.0,
        embossed_height_mm=1.0,
        bridge_span_mm=None,
        min_hole_diameter_mm=1.5,
        connecting_clearance_mm=0.3,  # moving parts clearance
        escape_hole_diameter_mm=5.0,  # powder removal
        min_feature_mm=0.8,
        pin_diameter_mm=0.8,
        tolerance_percent=0.3,
        tolerance_lower_mm=0.3,
    ),
    # ── Multi Jet Fusion (powder bed — no supports needed) ─────────────────
    "MJF": PrintingDesignRules(
        supported_wall_mm=0.7,
        unsupported_wall_mm=None,
        max_overhang_deg=None,
        embossed_width_mm=1.0,
        embossed_height_mm=1.0,
        bridge_span_mm=None,
        min_hole_diameter_mm=1.5,
        connecting_clearance_mm=0.3,
        escape_hole_diameter_mm=5.0,
        min_feature_mm=0.8,
        pin_diameter_mm=0.8,
        tolerance_percent=0.3,
        tolerance_lower_mm=0.3,
    ),
    # ── Material Jetting (PolyJet, etc.) ──────────────────────────────────
    "MJ": PrintingDesignRules(
        supported_wall_mm=1.0,
        unsupported_wall_mm=1.0,
        max_overhang_deg=None,  # support always required
        embossed_width_mm=0.5,
        embossed_height_mm=0.5,
        bridge_span_mm=None,
        min_hole_diameter_mm=0.5,
        connecting_clearance_mm=0.2,
        escape_hole_diameter_mm=None,
        min_feature_mm=0.5,
        pin_diameter_mm=0.5,
        tolerance_percent=0.1,
        tolerance_lower_mm=0.1,
    ),
    # ── Binder Jetting ─────────────────────────────────────────────────────
    "BJ": PrintingDesignRules(
        supported_wall_mm=2.0,
        unsupported_wall_mm=3.0,
        max_overhang_deg=None,  # powder bed — no overhang concern
        embossed_width_mm=0.5,
        embossed_height_mm=0.5,
        bridge_span_mm=None,
        min_hole_diameter_mm=1.5,
        connecting_clearance_mm=None,
        escape_hole_diameter_mm=5.0,
        min_feature_mm=2.0,
        pin_diameter_mm=2.0,
        tolerance_percent=0.2,
        tolerance_lower_mm=0.2,  # ±0.2mm metal, ±0.3mm sand
    ),
    # ── Direct Metal Laser Sintering (DMLS) ───────────────────────────────
    "DMLS": PrintingDesignRules(
        supported_wall_mm=0.4,
        unsupported_wall_mm=0.5,
        max_overhang_deg=None,  # support always required for metal
        embossed_width_mm=0.1,
        embossed_height_mm=0.1,
        bridge_span_mm=2.0,
        min_hole_diameter_mm=1.5,
        connecting_clearance_mm=None,
        escape_hole_diameter_mm=5.0,
        min_feature_mm=0.6,
        pin_diameter_mm=1.0,
        tolerance_percent=0.1,
        tolerance_lower_mm=0.1,
    ),
}


# ---------------------------------------------------------------------------
# CNC Milling Design Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MillingDesignRules:
    """Design rules for CNC milling operations.

    All dimensional values are in mm unless otherwise noted.
    """

    # Minimum wall thickness — depends on material class
    min_wall_mm_metal: float = 0.8
    min_wall_mm_plastic: float = 1.5

    # Pocket / cavity depth rules
    # Recommended max cavity depth = ratio × cavity width
    cavity_depth_ratio: float = 4.0

    # Internal corner radius constraints
    # Absolute minimum internal radius (limited by smallest practical endmill)
    min_internal_radius_mm: float = 1.0
    # Recommended: internal radius ≥ 1/3 × cavity depth
    internal_radius_depth_ratio: float = 0.33

    # Floor radii achievable with ball endmills (standard sizes)
    floor_radius_recommended_mm: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 3.0)

    # Hole constraints
    min_hole_diameter_mm: float = 1.0
    # Hole depth limits (multiples of nominal diameter)
    hole_depth_recommended_ratio: float = 4.0  # standard
    hole_depth_typical_ratio: float = 10.0  # achievable
    hole_depth_feasible_ratio: float = 40.0  # requires special tooling

    # Thread constraints
    min_thread_nominal_diameter_mm: float = 2.5  # M2.5
    thread_length_min_ratio: float = 1.5  # min engaged length = 1.5× nominal
    thread_length_recommended_ratio: float = 3.0  # recommended = 3× nominal

    # Text and lettering
    min_text_height_mm: float = 5.0  # prefer emboss over deboss

    # Undercut constraints (T-slots, dovetails)
    undercut_min_width_mm: float = 3.0
    undercut_max_width_mm: float = 40.0
    undercut_dovetail_angles_deg: tuple[float, ...] = (45.0, 60.0)


MILLING_RULES = MillingDesignRules()


# Tool diameter reference table.
# Each entry: (tool_diameter_mm, standard_length_mm, max_length_mm, min_corner_radius_mm)  # noqa: E501
# Source: standard endmill tooling specifications.
# Higher lengths possible with tool holder extensions (not recommended).
TOOL_DIAMETER_TABLE: list[tuple[float, float, float, float]] = [
    (2.0, 8.0, 10.0, 1.5),
    (3.0, 12.0, 15.0, 2.0),
    (4.0, 15.0, 20.0, 2.5),
    (6.0, 25.0, 30.0, 3.5),
    (8.0, 35.0, 40.0, 4.5),
    (10.0, 45.0, 50.0, 5.5),
    (12.0, 55.0, 60.0, 6.5),
    (16.0, 75.0, 80.0, 8.5),
    (20.0, 95.0, 100.0, 10.5),
    (25.0, 120.0, 125.0, 13.0),
    (32.0, 155.0, 160.0, 17.0),
    (50.0, 240.0, 250.0, 27.0),
    (63.0, 305.0, 315.0, 35.0),
]


def get_tool_for_radius(internal_radius_mm: float) -> tuple[float, float, float] | None:
    """Return the smallest (diameter, standard_length, max_length) tool that
    can machine a given internal corner radius.

    Returns None if no standard tool can achieve the radius
    (radius is smaller than the smallest tool's min_corner_radius).
    """
    for diam, std_len, max_len, min_r in TOOL_DIAMETER_TABLE:
        if internal_radius_mm >= min_r:
            return (diam, std_len, max_len)
    return None


def get_min_radius_for_depth(cavity_depth_mm: float) -> float:
    """Return the minimum internal radius for a given cavity depth.

    Rule: internal radius ≥ max(1.0mm, 1/3 × cavity depth)
    """
    return max(
        MILLING_RULES.min_internal_radius_mm,
        cavity_depth_mm * MILLING_RULES.internal_radius_depth_ratio,
    )


# ---------------------------------------------------------------------------
# CNC Turning Design Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurningDesignRules:
    """Design rules for CNC turning operations.

    All dimensional values are in mm unless otherwise noted.
    """

    # Maximum length-to-diameter ratio without steady rest
    max_length_diameter_ratio: float = 8.0
    # Minimum shaft / feature outer diameter
    min_shaft_diameter_mm: float = 3.0
    # Minimum groove width (parting/grooving tool constraint)
    min_groove_width_mm: float = 3.0
    # Maximum groove depth relative to shaft radius
    groove_depth_max_ratio: float = 0.5
    # Minimum boring diameter
    min_bore_diameter_mm: float = 3.0
    # Maximum bore depth relative to bore diameter
    bore_depth_max_ratio: float = 4.0
    # Minimum thread nominal diameter
    min_thread_nominal_diameter_mm: float = 2.5
    # Minimum parting-off groove width
    parting_width_mm: float = 3.0


TURNING_RULES = TurningDesignRules()
