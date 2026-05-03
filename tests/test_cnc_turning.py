import trimesh

from src.core.cnc_analyzers import _compute_z_slice_profile, detect_axial_symmetry
from src.core.geometry import _analyze_single_process


def test_z_axis_cylinder_is_turnable_from_path2d_sections():
    """Z-axis cylinder sections returned as Path2D should still classify as turnable."""
    cylinder = trimesh.creation.cylinder(radius=25.0, height=15.0, sections=96)

    slice_profile = _compute_z_slice_profile(cylinder, n_slices=40)
    axis_report = detect_axial_symmetry(cylinder, slice_profile=slice_profile)

    assert len(slice_profile[0]) > 0
    assert axis_report.is_turnable
    assert axis_report.primary_axis == "Z"
    assert axis_report.symmetry_deviation < 0.15


def test_x_axis_cylinder_detects_turning_axis():
    """X-aligned turned profiles classify without rotating the uploaded model."""
    cylinder = trimesh.creation.cylinder(radius=2.75, height=162.0, sections=96)
    cylinder.apply_transform(
        trimesh.transformations.rotation_matrix(1.57079632679, [0, 1, 0])
    )

    axis_report = detect_axial_symmetry(cylinder)

    assert axis_report.is_turnable
    assert axis_report.primary_axis == "X"
    assert axis_report.axis_vector == [1.0, 0.0, 0.0]
    assert axis_report.symmetry_deviation < 0.15


def test_cnc_turn_report_exposes_detected_x_axis():
    """CNC_TURN report should carry detected axis metadata for viewer annotations."""
    cylinder = trimesh.creation.cylinder(radius=2.75, height=162.0, sections=96)
    cylinder.apply_transform(
        trimesh.transformations.rotation_matrix(1.57079632679, [0, 1, 0])
    )

    report = _analyze_single_process(cylinder.export(file_type="stl"), "CNC_TURN")

    assert report["isTurnable"]
    assert report["primaryAxis"] == "X"
    assert report["axisVector"] == [1.0, 0.0, 0.0]
    assert report["symmetryDeviation"] < 0.15


def test_axis_point_tracks_round_bore_not_bounding_box_center():
    """Turning axis origin should come from round feature geometry, not bbox center."""
    tube = trimesh.creation.annulus(
        r_min=2.0,
        r_max=7.5,
        height=30.0,
        sections=128,
    )
    tube.apply_translation([12.0, -4.0, 0.0])

    protrusion = trimesh.creation.box(extents=[14.0, 3.0, 4.0])
    protrusion.apply_translation([30.0, -4.0, 0.0])
    mesh = trimesh.util.concatenate([tube, protrusion])

    axis_report = detect_axial_symmetry(mesh, symmetry_threshold=0.35)
    bbox_center = mesh.bounds.mean(axis=0)

    assert axis_report.axis_point is not None
    assert abs(axis_report.axis_point[0] - 12.0) < 0.25
    assert abs(axis_report.axis_point[1] + 4.0) < 0.25
    assert abs(axis_report.axis_point[0] - bbox_center[0]) > 3.0


def test_cnc_turn_report_exposes_axis_point():
    """CNC_TURN report should include a model-space point on the turning axis."""
    cylinder = trimesh.creation.cylinder(radius=8.0, height=32.0, sections=96)
    cylinder.apply_translation([10.0, -6.0, 0.0])

    report = _analyze_single_process(cylinder.export(file_type="stl"), "CNC_TURN")

    assert report["isTurnable"]
    assert report["primaryAxis"] == "Z"
    assert report["axisVector"] == [0.0, 0.0, 1.0]
    assert report["axisPoint"] == [10.0, -6.0, 0.0]
