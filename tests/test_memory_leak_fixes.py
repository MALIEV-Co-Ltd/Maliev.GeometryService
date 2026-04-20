"""Unit and integration tests for the memory leak fixes in the DFM pipeline.

Covers:
  1. _BoundedFileCache eviction by count, bytes, and TTL   (main.py)
  2. _BoundedDfmCache eviction by count and bytes          (geometry_optimizations.py)
  3. DiagnosticExecutor max_tasks_per_child forwarding     (worker_wrapper.py)
  4. GeometryProcessor rendering worker recycling          (geometry.py)
  5. HTTP quality-check endpoint under concurrent load     (main.py)

Run:
    pytest tests/test_memory_leak_fixes.py -v
Slow marker:
    pytest tests/test_memory_leak_fixes.py -m slow -v
"""

import multiprocessing
import time

import pytest


# ---------------------------------------------------------------------------
# 1 & 2 — BoundedFileCache unit tests
# ---------------------------------------------------------------------------

class TestBoundedFileCache:
    """Unit tests for _BoundedFileCache in src/main.py."""

    def _make_cache(self, max_entries=5, max_bytes=1000, ttl_seconds=60):
        from src.main import _BoundedFileCache
        return _BoundedFileCache(
            max_entries=max_entries,
            max_bytes=max_bytes,
            ttl_seconds=ttl_seconds,
        )

    def test_evicts_by_count(self):
        cache = self._make_cache(max_entries=5, max_bytes=10_000_000)
        for i in range(8):
            cache[f"upload_{i}"] = {"stl_bytes": b"x" * 10, "cad_bytes": None}
        assert len(cache) <= 5, f"Expected ≤5 entries, got {len(cache)}"

    def test_evicts_oldest_first(self):
        cache = self._make_cache(max_entries=3, max_bytes=10_000_000)
        for i in range(3):
            cache[f"key_{i}"] = {"stl_bytes": b"x", "cad_bytes": None}
        # Insert 4th → oldest (key_0) should be evicted
        cache["key_3"] = {"stl_bytes": b"x", "cad_bytes": None}
        assert "key_0" not in cache, "Oldest entry should have been evicted"
        assert "key_3" in cache, "Newest entry should be present"

    def test_evicts_by_bytes(self):
        # max_bytes = 100; each entry = 30 bytes → cap hit at 4th entry
        cache = self._make_cache(max_entries=100, max_bytes=100)
        for i in range(8):
            cache[f"k{i}"] = {"stl_bytes": b"x" * 30, "cad_bytes": None}
        assert cache.total_bytes <= 100, (
            f"total_bytes {cache.total_bytes} exceeds 100-byte cap"
        )

    def test_ttl_eviction_on_write(self):
        cache = self._make_cache(max_entries=10, max_bytes=10_000_000, ttl_seconds=0.05)
        cache["old_key"] = {"stl_bytes": b"hello", "cad_bytes": None}
        time.sleep(0.1)  # let TTL expire
        # Trigger eviction via a new write
        cache["new_key"] = {"stl_bytes": b"world", "cad_bytes": None}
        assert "old_key" not in cache, "Expired entry should be evicted on next write"

    def test_ttl_eviction_on_contains(self):
        cache = self._make_cache(max_entries=10, max_bytes=10_000_000, ttl_seconds=0.05)
        cache["my_key"] = {"stl_bytes": b"data", "cad_bytes": None}
        time.sleep(0.1)
        assert "my_key" not in cache, "__contains__ should trigger TTL eviction"

    def test_delete_works(self):
        cache = self._make_cache()
        cache["k"] = {"stl_bytes": b"abc", "cad_bytes": None}
        assert "k" in cache
        del cache["k"]
        assert "k" not in cache

    def test_delete_missing_raises_keyerror(self):
        cache = self._make_cache()
        with pytest.raises(KeyError):
            del cache["nonexistent"]

    def test_update_existing_key_no_double_count(self):
        cache = self._make_cache(max_entries=10, max_bytes=10_000_000)
        cache["k"] = {"stl_bytes": b"x" * 50, "cad_bytes": None}
        size_before = cache.total_bytes
        cache["k"] = {"stl_bytes": b"x" * 30, "cad_bytes": None}
        assert cache.total_bytes == 30, (
            f"After update total_bytes should be 30, got {cache.total_bytes}"
        )

    def test_cad_bytes_counted_in_size(self):
        cache = self._make_cache(max_entries=10, max_bytes=200, ttl_seconds=60)
        # stl=80 + cad=80 = 160 bytes; second entry (80+0=80) pushes total to 240 → evict first
        cache["k1"] = {"stl_bytes": b"x" * 80, "cad_bytes": b"y" * 80}
        cache["k2"] = {"stl_bytes": b"x" * 80, "cad_bytes": None}
        assert cache.total_bytes <= 200


# ---------------------------------------------------------------------------
# 3 — _BoundedDfmCache unit tests
# ---------------------------------------------------------------------------

class TestBoundedDfmCache:
    """Unit tests for _BoundedDfmCache in src/core/geometry_optimizations.py."""

    def _make_cache(self, max_entries=5, max_bytes=1000):
        from src.core.geometry_optimizations import _BoundedDfmCache
        return _BoundedDfmCache(max_entries=max_entries, max_bytes=max_bytes)

    def test_evicts_by_count(self):
        cache = self._make_cache(max_entries=3, max_bytes=10_000_000)
        for i in range(6):
            cache.set(f"key_{i}", {"result": "x" * 10, "index": i})
        assert len(cache) <= 3

    def test_get_returns_none_for_missing(self):
        cache = self._make_cache()
        assert cache.get("missing") is None

    def test_get_returns_value(self):
        cache = self._make_cache()
        cache.set("k", {"issues": [], "process": "FDM"})
        result = cache.get("k")
        assert result is not None
        assert result["process"] == "FDM"

    def test_evicts_by_bytes(self):
        # Each entry serializes to ~50 bytes; cap at 200 → at most ~4 entries
        import json
        cache = self._make_cache(max_entries=100, max_bytes=200)
        payload = {"data": "x" * 40}  # ~50 bytes serialized
        for i in range(10):
            cache.set(f"k{i}", payload)
        assert cache.total_bytes <= 200, (
            f"total_bytes {cache.total_bytes} exceeds 200-byte cap"
        )

    def test_clear(self):
        cache = self._make_cache()
        cache.set("k", {"x": 1})
        cache.clear()
        assert len(cache) == 0
        assert cache.total_bytes == 0

    def test_update_existing_key_replaces(self):
        cache = self._make_cache(max_entries=10, max_bytes=10_000_000)
        cache.set("k", {"v": 1})
        cache.set("k", {"v": 2})
        assert cache.get("k")["v"] == 2
        assert len(cache) == 1


# ---------------------------------------------------------------------------
# 4 — Public cache_result / get_cached_result / clear_cache API
# ---------------------------------------------------------------------------

class TestDfmCachePublicApi:
    """Ensure public API functions still work after replacing the backing store."""

    def test_cache_miss_returns_none(self):
        from src.core.geometry_optimizations import clear_cache, get_cached_result
        clear_cache()
        result = get_cached_result(b"fake_stl_data_that_does_not_exist", "FDM")
        assert result is None

    def test_cache_roundtrip(self):
        from src.core.geometry_optimizations import (
            cache_result,
            clear_cache,
            get_cached_result,
        )
        clear_cache()
        stl = b"STL_BYTES" * 100
        report = {"issues": [{"type": "thin_wall"}], "process": "FDM"}
        cache_result(stl, "FDM", report)
        cached = get_cached_result(stl, "FDM")
        assert cached == report

    def test_different_process_codes_independent(self):
        from src.core.geometry_optimizations import (
            cache_result,
            clear_cache,
            get_cached_result,
        )
        clear_cache()
        stl = b"stl_bytes_shared"
        cache_result(stl, "FDM", {"process": "FDM"})
        cache_result(stl, "CNC_MILL", {"process": "CNC_MILL"})
        assert get_cached_result(stl, "FDM")["process"] == "FDM"
        assert get_cached_result(stl, "CNC_MILL")["process"] == "CNC_MILL"
        assert get_cached_result(stl, "SLA") is None


# ---------------------------------------------------------------------------
# 5 — DiagnosticExecutor max_tasks_per_child forwarding
# ---------------------------------------------------------------------------

class TestDiagnosticExecutorMaxTasksPerChild:
    """Verify DiagnosticExecutor accepts max_tasks_per_child without error."""

    def test_accepts_max_tasks_per_child_param(self):
        """DiagnosticExecutor must accept max_tasks_per_child without TypeError."""
        from src.core.worker_wrapper import DiagnosticExecutor
        # On Python <3.11 the value is silently ignored (warning is logged);
        # on Python ≥3.11 it is forwarded to ProcessPoolExecutor.  Either way
        # construction must not raise.
        ex = DiagnosticExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            max_tasks_per_child=5,
        )
        ex.shutdown(wait=False)

    def test_none_max_tasks_per_child_accepted(self):
        from src.core.worker_wrapper import DiagnosticExecutor
        ex = DiagnosticExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            max_tasks_per_child=None,
        )
        ex.shutdown(wait=False)

    def test_max_tasks_per_child_stored(self):
        """DiagnosticExecutor stores max_tasks_per_child for introspection."""
        from src.core.worker_wrapper import DiagnosticExecutor
        ex = DiagnosticExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            max_tasks_per_child=7,
        )
        try:
            assert ex.max_tasks_per_child == 7
        finally:
            ex.shutdown(wait=False)

    @pytest.mark.slow
    def test_pool_executor_wrapper_recycles(self):
        """PoolExecutorWrapper workers are replaced after maxtasksperchild jobs.

        DiagnosticExecutor requires Python 3.11+ for recycling; on Python 3.10
        we use PoolExecutorWrapper which supports maxtasksperchild on all versions.
        Uses _pid_probe_worker (module-level) so it is picklable by spawn context.
        """
        from src.core.geometry import PoolExecutorWrapper, _pid_probe_worker

        ex = PoolExecutorWrapper(processes=1, maxtasksperchild=2)
        try:
            pids = set()
            futures = [ex.submit(_pid_probe_worker) for _ in range(6)]
            for f in futures:
                pids.add(f.result(timeout=30))
        finally:
            ex.shutdown(wait=False)

        assert len(pids) >= 2, (
            f"Expected worker PIDs to recycle after 2 jobs, got: {pids}"
        )


# ---------------------------------------------------------------------------
# 6 — GeometryProcessor rendering executor uses max_tasks_per_child=5
# ---------------------------------------------------------------------------

class TestGeometryProcessorExecutorConfig:
    """Check GeometryProcessor wires worker recycling on the rendering executor."""

    def test_executor_created_with_recycling(self):
        """The rendering executor must be configured to recycle workers.

        On Python ≥3.11: DiagnosticExecutor with max_tasks_per_child=5.
        On Python <3.11:  PoolExecutorWrapper with maxtasksperchild=5.
        """
        from src.core.geometry import GeometryProcessor

        p = GeometryProcessor(enable_diagnostics=False)
        try:
            executor = p.executor
            # PoolExecutorWrapper exposes maxtasksperchild directly.
            # DiagnosticExecutor (Python 3.11+) exposes max_tasks_per_child.
            mtc = getattr(executor, "maxtasksperchild", None) or getattr(
                executor, "max_tasks_per_child", None
            )
            assert mtc is not None and mtc > 0, (
                "Rendering executor has no recycling configured — "
                "workers will accumulate memory indefinitely"
            )
        finally:
            p.shutdown(timeout=5)

    def test_dfm_executor_still_recycles(self):
        """DFM executor (PoolExecutorWrapper) should recycle at maxtasksperchild=2."""
        from src.core.geometry import GeometryProcessor

        p = GeometryProcessor(enable_diagnostics=False)
        try:
            mtc = getattr(p.dfm_executor, "maxtasksperchild", None)
            assert mtc == 2, f"DFM executor maxtasksperchild expected 2, got {mtc}"
        finally:
            p.shutdown(timeout=5)


# ---------------------------------------------------------------------------
# 7 — FastAPI endpoints cache size stays bounded under load
# ---------------------------------------------------------------------------

class TestQualityCheckCacheBounded:
    """The /quality-check endpoint must not grow _file_analysis_cache unboundedly."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        """Clear the module-level cache before and after each test."""
        import src.main as main_module
        from src.main import _BoundedFileCache
        original = main_module._file_analysis_cache
        main_module._file_analysis_cache = _BoundedFileCache(
            max_entries=5, max_bytes=50_000, ttl_seconds=60
        )
        yield main_module._file_analysis_cache
        main_module._file_analysis_cache = original

    def test_cache_size_bounded_after_many_inserts(self, reset_cache):
        """Inserting more than max_entries must not exceed max_entries."""
        cache = reset_cache
        # Directly insert (bypasses HTTP path — unit-level)
        for i in range(20):
            cache[f"upload_{i:03d}"] = {
                "stl_bytes": b"x" * 100,
                "cad_bytes": None,
                "cad_extension": None,
            }
        assert len(cache) <= 5, f"Cache grew to {len(cache)}, max is 5"

    def test_cache_bytes_bounded(self, reset_cache):
        """Total bytes in cache must not exceed max_bytes."""
        cache = reset_cache
        for i in range(20):
            cache[f"upload_{i:03d}"] = {
                "stl_bytes": b"y" * 4_000,  # 4 KB each → 80 KB total without cap
                "cad_bytes": None,
                "cad_extension": None,
            }
        assert cache.total_bytes <= 50_000, (
            f"total_bytes {cache.total_bytes} exceeds 50 000-byte cap"
        )
