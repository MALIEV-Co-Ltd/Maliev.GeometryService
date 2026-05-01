import trimesh

from src.core.cnc_analyzers import _compute_z_slice_profile, detect_axial_symmetry


def test_z_axis_cylinder_is_turnable_from_path2d_sections():
    """Z-axis cylinder sections returned as Path2D should still classify as turnable."""
    cylinder = trimesh.creation.cylinder(radius=25.0, height=15.0, sections=96)

    slice_profile = _compute_z_slice_profile(cylinder, n_slices=40)
    axis_report = detect_axial_symmetry(cylinder, slice_profile=slice_profile)

    assert len(slice_profile[0]) > 0
    assert axis_report.is_turnable
    assert axis_report.primary_axis == "Z"
    assert axis_report.symmetry_deviation < 0.15
