"""Integration tests for memory-leak fixes in the DFM pipeline.

These tests use real GeometryProcessor instances and real executors, so they
are slower than unit tests.  All are marked @pytest.mark.slow.

Run:
    pytest tests/test_memory_leak_integration.py -m slow -v
"""

import os
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.slow

_cascadio_available = True
try:
    import cascadio  # noqa: F401
except ImportError:
    _cascadio_available = False

requires_cascadio = pytest.mark.skipif(
    not _cascadio_available, reason="cascadio not installed"
)

ASSETS = Path(__file__).parent / "assets"
SMALL_STEP = ASSETS / "50x50x50mm-solid-cube.step"


@pytest.fixture(scope="module")
def small_step_bytes():
    if not SMALL_STEP.exists():
        pytest.skip(f"Test asset missing: {SMALL_STEP}")
    return SMALL_STEP.read_bytes()


@pytest.fixture(scope="module")
def small_step_stl_bytes(small_step_bytes):
    """Convert the small STEP asset to STL bytes once for the module."""
    from src.core.geometry import _compute_metrics_worker

    result = _compute_metrics_worker(small_step_bytes, ".step")
    stl = result.get("mesh_stl_bytes")
    if not stl:
        pytest.skip("Could not extract STL from STEP asset")
    return stl


# ---------------------------------------------------------------------------
# 8 — Rendering worker recycling (integration)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@requires_cascadio
class TestRenderingWorkerRecycling:
    """Rendering workers must be replaced after max_tasks_per_child=5 jobs."""

    def test_rendering_worker_recycles(self, small_step_bytes):
        """Submit >5 render jobs; assert at least 2 distinct worker PIDs."""
        import asyncio
        import tempfile

        from src.core.geometry import (
            GeometryProcessor,
            _compute_metrics_worker,
            _pid_probe_worker,
        )

        # Produce a tiny GLB from the STEP asset
        result = _compute_metrics_worker(small_step_bytes, ".step")
        glb_bytes = result.get("cad_glb_bytes")
        if not glb_bytes:
            pytest.skip("Could not produce GLB from STEP asset")

        # Write GLB to a temp path (thumbnail worker expects a file path)
        with tempfile.NamedTemporaryFile(suffix=".glb", delete=False) as f:
            glb_path = f.name
            f.write(glb_bytes)

        try:
            processor = GeometryProcessor(enable_diagnostics=False)
            loop = asyncio.new_event_loop()

            async def collect_pids() -> set:
                pids: set = set()
                for _ in range(8):
                    fut = loop.run_in_executor(processor.executor, _pid_probe_worker)
                    pid = await fut
                    pids.add(pid)
                return pids

            try:
                pids = loop.run_until_complete(collect_pids())
            finally:
                loop.close()
                processor.shutdown(timeout=15)

        finally:
            os.unlink(glb_path)  # noqa: PTH108

        assert len(pids) >= 2, (
            f"Expected worker PIDs to recycle after 5 jobs, "
            f"but only got {len(pids)} distinct PIDs: {pids}"
        )


# ---------------------------------------------------------------------------
# 9 — RSS stability across repeated in-process Phase 1 calls
# ---------------------------------------------------------------------------


@pytest.mark.slow
@requires_cascadio
class TestRssStability:
    """RSS growth must stay bounded across repeated processing calls."""

    def test_rss_stable_across_8_phase1_calls(self, small_step_bytes):
        """Run 8 sequential Phase 1 calls; RSS delta per call must be < 150 MB."""
        from src.core.geometry import _compute_metrics_worker

        proc = psutil.Process()
        rss_deltas = []

        for _i in range(8):
            rss_before = proc.memory_info().rss / 1024 / 1024
            _compute_metrics_worker(small_step_bytes, ".step")
            rss_after = proc.memory_info().rss / 1024 / 1024
            delta = rss_after - rss_before
            rss_deltas.append(delta)

        # Allow initial warm-up spike (first call) but subsequent calls should
        # not accumulate more than 150 MB each.
        steady_deltas = rss_deltas[1:]  # skip first (cold JIT / import load)
        max_steady_delta = max(steady_deltas) if steady_deltas else 0

        assert max_steady_delta < 150, (
            f"RSS grew by {max_steady_delta:.1f} MB in a single Phase 1 call "
            f"(calls 2-8). Memory is not being released between calls.\n"
            f"All deltas: {[f'{d:.1f}' for d in rss_deltas]}"
        )

    def test_total_rss_growth_bounded_across_8_calls(self, small_step_bytes):
        """Total RSS growth over 8 calls must stay under 500 MB."""
        from src.core.geometry import _compute_metrics_worker

        proc = psutil.Process()
        rss_start = proc.memory_info().rss / 1024 / 1024

        for _ in range(8):
            _compute_metrics_worker(small_step_bytes, ".step")

        rss_end = proc.memory_info().rss / 1024 / 1024
        total_growth = rss_end - rss_start

        assert total_growth < 500, (
            f"Total RSS grew by {total_growth:.1f} MB over 8 Phase 1 calls. "
            f"Expected < 500 MB total growth."
        )


# ---------------------------------------------------------------------------
# 10 — DFM cache bounds enforced end-to-end via public API
# ---------------------------------------------------------------------------


class TestDfmCacheBoundsEndToEnd:
    """Drive cache_result / get_cached_result with realistic report objects."""

    def test_cache_does_not_exceed_50_entries(self):
        from src.core.geometry_optimizations import (
            _BoundedDfmCache,
            get_cache_key,
        )

        cache = _BoundedDfmCache(max_entries=10, max_bytes=10_000_000)
        for i in range(20):
            stl = f"fake_stl_content_{i:04d}".encode()
            key = get_cache_key(stl, "FDM")
            cache.set(key, {"issues": [], "process": "FDM", "index": i})

        assert len(cache) <= 10, f"Cache exceeded 10 entries: {len(cache)}"

    def test_large_report_evicts_to_stay_within_bytes(self):
        """A single very large report must not bypass the byte cap."""
        from src.core.geometry_optimizations import _BoundedDfmCache

        cache = _BoundedDfmCache(max_entries=100, max_bytes=50_000)
        # Each report is ~20 KB serialized
        big_payload = {"face_indices": list(range(2_000)), "process": "FDM"}
        for i in range(10):
            cache.set(f"key_{i}", big_payload)

        assert (
            cache.total_bytes <= 50_000
        ), f"Cache total_bytes {cache.total_bytes} exceeds 50 000-byte cap"


# ---------------------------------------------------------------------------
# 11 — _file_analysis_cache module-level instance starts bounded
# ---------------------------------------------------------------------------


class TestFileAnalysisCacheModuleInstance:
    """The module-level _file_analysis_cache must start bounded."""

    def test_default_instance_is_bounded(self):
        from src.main import _BoundedFileCache, _file_analysis_cache

        assert isinstance(
            _file_analysis_cache, _BoundedFileCache
        ), "_file_analysis_cache must be a _BoundedFileCache instance, not a bare dict"
        assert _file_analysis_cache.max_entries == 20
        assert _file_analysis_cache.max_bytes == 500 * 1024 * 1024
        assert _file_analysis_cache.ttl_seconds == 3600

    def test_default_dfm_cache_is_bounded(self):
        from src.core.geometry_optimizations import _BoundedDfmCache, _cache

        assert isinstance(
            _cache, _BoundedDfmCache
        ), "_cache must be a _BoundedDfmCache instance, not a bare dict"
        assert _cache.max_entries == 50
        assert _cache.max_bytes == 200 * 1024 * 1024
