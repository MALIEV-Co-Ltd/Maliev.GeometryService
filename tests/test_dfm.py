import time

import numpy as np
import pytest
import trimesh

from src.core.dfm import DfmAnalyzer


class TestThinWallDetection:
    def test_thin_wall_positive(self) -> None:
        mesh = trimesh.creation.box(extents=[50.0, 50.0, 0.5])
        analyzer = DfmAnalyzer(sample_count=500, min_thickness_mm=0.8)
        count, regions = analyzer.detect_thin_walls(mesh)
        assert count > 0, "Expected thin walls to be detected in 0.5mm thick plate"
        assert len(regions) == count, "Region count should match sample count"
        for region in regions:
            assert len(region) == 3, "Each region should be a 3D coordinate"

    def test_thin_wall_negative(self) -> None:
        mesh = trimesh.creation.box(extents=[10.0, 10.0, 10.0])
        analyzer = DfmAnalyzer(sample_count=500, min_thickness_mm=0.8)
        count, regions = analyzer.detect_thin_walls(mesh)
        assert count == 0, "Expected no thin walls in 10mm cube"
        assert regions == [], "Regions should be empty"


class TestOverhangDetection:
    def test_overhang_positive(self) -> None:
        mesh = trimesh.creation.box(extents=[20.0, 20.0, 10.0])
        analyzer = DfmAnalyzer(critical_angle_deg=45.0)
        count, area = analyzer.detect_overhangs(mesh)
        assert count > 0, "Expected overhang faces on box with bottom face"
        assert area > 0, "Expected non-zero overhang area"

    def test_overhang_negative(self) -> None:
        vertices = np.array(
            [
                [0, 0, 0],
                [1, 0, 0],
                [1, 1, 0],
                [0, 1, 0],
                [0, 0, 1],
                [1, 0, 1],
                [1, 1, 1],
                [0, 1, 1],
            ],
            dtype=np.float64,
        )
        faces = np.array(
            [
                [0, 1, 2],
                [0, 2, 3],
                [4, 5, 6],
                [4, 6, 7],
                [0, 1, 5],
                [0, 5, 4],
                [2, 3, 7],
                [2, 7, 6],
                [0, 3, 7],
                [0, 7, 4],
                [1, 2, 6],
                [1, 6, 5],
            ]
        )
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces)
        mesh.apply_transform(
            trimesh.transformations.rotation_matrix(np.pi / 4, [1, 0, 0])
        )
        analyzer = DfmAnalyzer(critical_angle_deg=45.0)
        count, area = analyzer.detect_overhangs(mesh)
        assert count == 0, "Expected no overhangs in rotated mesh"


class TestDegenerateMesh:
    def test_degenerate_empty_mesh(self) -> None:
        mesh = trimesh.Trimesh(
            vertices=np.array([]).reshape(0, 3), faces=np.array([]).reshape(0, 3)
        )
        analyzer = DfmAnalyzer()
        report = analyzer.analyze(mesh)
        assert report.thin_wall_count == 0
        assert report.overhang_face_count == 0
        assert report.overhang_area_cm2 == 0.0

    def test_degenerate_exception_handling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mesh = trimesh.creation.box(extents=[10.0, 10.0, 10.0])

        def raise_error(
            _self: object, _m: trimesh.Trimesh
        ) -> tuple[int, list[list[float]]]:
            raise RuntimeError("Simulated error")

        analyzer = DfmAnalyzer()
        monkeypatch.setattr(DfmAnalyzer, "detect_thin_walls", raise_error)
        report = analyzer.analyze(mesh)
        assert report.thin_wall_count == 0
        assert report.overhang_face_count == 0


class TestDfmReportSerialization:
    def test_camel_case_aliases(self) -> None:
        from src.core.dfm import DfmReport

        report = DfmReport(
            thinWallCount=5,
            thinWallRegions=[[1.0, 2.0, 3.0]],
            overhangFaceCount=10,
            overhangAreaCm2=2.5,
        )
        json_str = report.model_dump_json(by_alias=True)
        assert "thinWallCount" in json_str
        assert "thinWallRegions" in json_str
        assert "overhangFaceCount" in json_str
        assert "overhangAreaCm2" in json_str


class TestPerformance:
    def test_performance_100k_triangles(self) -> None:
        mesh = trimesh.creation.icosphere(subdivisions=5)
        mesh = mesh.subdivide()
        actual_triangles = len(mesh.faces)
        assert (
            actual_triangles < 100000
        ), f"Test mesh should be under 100k triangles, got {actual_triangles}"
        analyzer = DfmAnalyzer(sample_count=500)
        start = time.perf_counter()
        _ = analyzer.analyze(mesh)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"DFM analysis took {elapsed:.2f}s, expected < 5s"
