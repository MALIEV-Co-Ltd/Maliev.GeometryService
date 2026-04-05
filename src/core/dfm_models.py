"""
Unified DFM issue and report data models for the GeometryService.

These are internal Python dataclasses used during analysis. They are
serialised to Pydantic models before being sent over the message bus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DfmSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class DfmIssue:
    """A single DFM issue detected during analysis.

    Attributes:
        category:     Short machine-readable key, e.g. ``"thin_wall"``,
                      ``"internal_radius"``, ``"hole_depth"``.
        severity:     ``"info"`` / ``"warning"`` / ``"error"``
        title:        Short human-readable title shown in the overlay panel.
        description:  Detailed message including measured vs. required values.
        value:        Measured value for this issue (mm, deg, ratio, etc.).
        threshold:    Rule threshold that was violated.
        face_indices: Triangle face indices in the tessellated mesh that
                      correspond to this issue (used to generate overlay GLBs).
        centroid:     Representative [x, y, z] position in mm (model space).
        metadata:     Process-specific extra data, e.g.
                      ``{"tool_diameter_mm": 3.0, "depth_ratio": 5.2}``.
    """

    category: str
    severity: DfmSeverity
    title: str
    description: str
    value: float | None = None
    threshold: float | None = None
    face_indices: list[int] = field(default_factory=list)
    centroid: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    metadata: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Intermediate analysis result containers
# ---------------------------------------------------------------------------


@dataclass
class HoleFeature:
    """A detected hole (cylindrical void) in the mesh."""

    center: list[float]        # [x, y, z] hole centre in mm
    axis: list[float]          # unit vector along hole axis
    diameter_mm: float
    depth_mm: float
    face_indices: list[int]    # mesh faces forming the cylinder wall


@dataclass
class InternalRadiusFeature:
    """A concave edge region with a measurable fillet / corner radius."""

    radius_mm: float           # measured or absent (0.0 = sharp corner)
    centroid: list[float]      # [x, y, z]
    depth_mm: float            # depth of the containing cavity (for ratio check)
    face_indices: list[int]


@dataclass
class CavityFeature:
    """A pocket or cavity in the part (relevant for CNC milling)."""

    width_mm: float
    depth_mm: float
    centroid: list[float]
    face_indices: list[int]

    @property
    def depth_ratio(self) -> float:
        return self.depth_mm / self.width_mm if self.width_mm > 0 else 0.0


@dataclass
class AxisSymmetryReport:
    """Result of testing whether a mesh is a surface of revolution."""

    is_turnable: bool
    primary_axis: str | None          # "X", "Y", or "Z"
    axis_vector: list[float]          # unit vector
    length_diameter_ratio: float | None
    symmetry_deviation: float         # 0 = perfect, >0 = deviation from round


@dataclass
class GrooveFeature:
    """A circumferential groove on a turned part."""

    width_mm: float
    depth_mm: float
    diameter_at_groove_mm: float
    centroid: list[float]
    face_indices: list[int]


@dataclass
class ToolAccessReport:
    """Result of 3/4/5-axis accessibility analysis."""

    minimum_axes: int              # 3, 4, or 5
    inaccessible_face_count: int
    inaccessible_face_indices: list[int]
    details: str                   # human-readable summary
