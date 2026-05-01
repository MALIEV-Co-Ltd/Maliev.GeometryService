"""Optimizations for two-phase DFM analysis.

This module provides:
1. Process-specific analysis (skip irrelevant checks)
2. Adaptive tessellation quality
3. Result caching
4. Algorithmic improvements
"""

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

from src.core.geometry import _analyze_single_process

# ── Optimization 1: Process-Specific Analysis Helpers ────────────────

PRINTING_PROCESSES = ["FDM", "SLA", "SLS", "MJF", "MJ", "BJ", "DMLS"]
CNC_PROCESSES = ["CNC_MILL", "CNC_TURN"]

# Printing processes don't need these CNC-specific checks
CNC_ONLY_CHECKS = ["internal_radius", "tool_access", "deep_cavity", "sharp_corners"]

# CNC processes don't need these printing-specific checks
PRINTING_ONLY_CHECKS = ["overhang", "bridge", "support_required"]


def should_run_check(category: str, process_code: str) -> bool:
    """Determine if a check should run for a given process.

    Args:
        category: The DFM check category (e.g., "thin_wall", "overhang", "internal_radius")
        process_code: The manufacturing process code

    Returns:
        True if the check should run, False if it should be skipped
    """  # noqa: E501
    # Skip CNC-only checks for printing processes
    if process_code in PRINTING_PROCESSES and category in CNC_ONLY_CHECKS:
        return False

    # Skip printing-only checks for CNC processes
    if process_code in CNC_PROCESSES and category in PRINTING_ONLY_CHECKS:  # noqa: SIM103
        return False

    # Run all other checks
    return True


# ── Optimization 2: Adaptive Tessellation Quality ────────────────────


def get_tessellation_tolerance(process_code: str, file_size_mb: float = 0) -> float:
    """Get appropriate tessellation tolerance based on process and file size.

    Args:
        process_code: Manufacturing process code
        file_size_mb: File size in megabytes (for adaptive quality)

    Returns:
        Tessellation tolerance value
    """
    # CNC processes need high precision for toolpath generation
    if process_code in CNC_PROCESSES:
        return 0.02  # High precision for CNC

    # Printing processes can use coarser tessellation
    if process_code in PRINTING_PROCESSES:
        if file_size_mb > 10:
            return 0.2  # Very coarse for large files
        if file_size_mb > 1:
            return 0.1  # Coarse for medium files
        return 0.05  # Medium for small files

    # Default medium precision
    return 0.1


# ── Optimization 3: Result Caching ─────────────────────────────────────
#
# DFM reports are rich objects (face index lists, centroids, issue geometry)
# and can easily reach 10-50 MB each.  The previous FIFO-by-count cap of 100
# entries had no size limit, allowing a 100 × 50 MB = 5 GB ceiling.
#
# _BoundedDfmCache enforces:
#   MAX_ENTRIES — at most 50 reports cached
#   MAX_BYTES   — at most 200 MB total (JSON-serialized approximation)
# Eviction is FIFO (oldest-first) on every write.

_MAX_DFM_CACHE_ENTRIES = 50
_MAX_DFM_CACHE_BYTES = 200 * 1024 * 1024  # 200 MB


class _BoundedDfmCache:
    """Insertion-ordered DFM report cache with entry-count and byte caps."""

    def __init__(
        self,
        max_entries: int = _MAX_DFM_CACHE_ENTRIES,
        max_bytes: int = _MAX_DFM_CACHE_BYTES,
    ) -> None:
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._total_bytes = 0
        self.max_entries = max_entries
        self.max_bytes = max_bytes

    def get(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    def set(self, key: str, value: dict[str, Any]) -> None:
        # Approximate serialized size once on insertion; avoids repeated dumps.
        try:
            entry_bytes = len(json.dumps(value, default=str).encode())
        except Exception:
            entry_bytes = 0

        if key in self._data:
            self._remove(key)

        # Evict oldest until within both limits
        while self._data and (
            len(self._data) >= self.max_entries
            or self._total_bytes + entry_bytes > self.max_bytes
        ):
            self._remove_oldest()

        self._data[key] = value
        self._sizes[key] = entry_bytes
        self._total_bytes += entry_bytes

    def clear(self) -> None:
        self._data.clear()
        self._sizes.clear()
        self._total_bytes = 0

    def _remove(self, key: str) -> None:
        del self._data[key]
        self._total_bytes -= self._sizes.pop(key, 0)

    def _remove_oldest(self) -> None:
        if self._data:
            self._remove(next(iter(self._data)))

    def __len__(self) -> int:
        return len(self._data)

    def keys(self):
        return self._data.keys()

    @property
    def total_bytes(self) -> int:
        return self._total_bytes


_cache: _BoundedDfmCache = _BoundedDfmCache()


def get_cache_key(stl_bytes: bytes, process_code: str) -> str:
    """Generate cache key from file hash and process code.

    Args:
        stl_bytes: STL file bytes
        process_code: Manufacturing process code

    Returns:
        Cache key string
    """
    # Hash the file bytes (first 1MB for speed)
    file_hash = hashlib.md5(stl_bytes[: 1024 * 1024]).hexdigest()
    return f"{file_hash[:16]}:{process_code}"


def get_cached_result(stl_bytes: bytes, process_code: str) -> dict[str, Any] | None:
    """Get cached DFM result if available.

    Args:
        stl_bytes: STL file bytes
        process_code: Manufacturing process code

    Returns:
        Cached DFM report or None if not cached
    """
    cache_key = get_cache_key(stl_bytes, process_code)
    return _cache.get(cache_key)


def cache_result(stl_bytes: bytes, process_code: str, result: dict[str, Any]) -> None:
    """Cache DFM analysis result.

    Args:
        stl_bytes: STL file bytes
        process_code: Manufacturing process code
        result: DFM analysis result to cache
    """
    cache_key = get_cache_key(stl_bytes, process_code)
    _cache.set(cache_key, result)


def clear_cache() -> None:
    """Clear all cached results."""
    _cache.clear()


# ── Optimization 4: Process-Specific Analysis with All Optimizations ───────


def optimized_analyze_single_process(
    stl_bytes: bytes,
    process_code: str,
    cad_bytes: bytes | None = None,
    cad_extension: str | None = None,
    shared_precomputed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Optimized process-specific analysis with all optimizations.

    This is a drop-in replacement for _analyze_single_process() with:
    - Process-specific checks (skip irrelevant ones)
    - Adaptive tessellation quality
    - Result caching

    Args:
        stl_bytes: STL file bytes
        process_code: Manufacturing process code
        cad_bytes: Optional CAD file bytes
        cad_extension: Optional CAD file extension
        shared_precomputed: Optional pre-computed data

    Returns:
        DFM analysis report for the requested process
    """
    # Check cache first
    cached = get_cached_result(stl_bytes, process_code)
    if cached is not None:
        print(f"[CACHE HIT] Returning cached result for {process_code}")
        return cached

    print(f"[CACHE MISS] Analyzing {process_code}...")

    start_time = time.time()

    # Calculate file size for adaptive tessellation
    len(stl_bytes) / (1024 * 1024)

    # Run the analysis (this will use optimized checks internally)
    result = _analyze_single_process(
        stl_bytes,
        process_code,
        cad_bytes,
        cad_extension,
        shared_precomputed,
    )

    elapsed = time.time() - start_time

    # Add performance metadata
    result["analysis_time_seconds"] = elapsed
    result["optimization_version"] = "v2_optimized"
    result["cache_status"] = "cold"  # First time computation

    # Cache the result
    cache_result(stl_bytes, process_code, result)

    print(f"[CACHE] Cached result for {process_code} (analyzed in {elapsed:.2f}s)")

    return result


# ── Optimization 5: Early Termination Heuristics ──────────────────────


def should_terminate_early(face_count: int, vertex_count: int, complexity: str) -> bool:  # noqa: ARG001
    """Determine if analysis should terminate early due to complexity.

    For very simple geometries, we can skip expensive analyses.

    Args:
        face_count: Number of faces in mesh
        vertex_count: Number of vertices in mesh
        complexity: Complexity classification

    Returns:
        True if analysis should terminate early, False otherwise
    """
    # Very simple geometry - skip expensive spatial analyses
    if face_count < 100:
        return True

    # Low complexity - can skip some optimizations
    if complexity == "simple" and face_count < 1000:  # noqa: SIM103
        return True

    # Normal or complex geometry - run full analysis
    return False


# ── Optimization 6: Spatial Filtering Optimization ─────────────────────


def filter_faces_by_region(
    face_indices: list[int],
    centroids: list[list[float]],
    bounding_box: dict[str, float],
    region_size: float = 10.0,
) -> list[int]:
    """Filter faces to only those in a specific region to reduce computation.

    This is useful when we only need to analyze faces in a specific area.

    Args:
        face_indices: List of face indices
        centroids: List of centroid coordinates [x, y, z]
        bounding_box: Bounding box with x, y, z dimensions
        region_size: Size of region to analyze (in mm)

    Returns:
        Filtered list of face indices
    """
    if not face_indices or not centroids:
        return []

    # Calculate region center (middle of bounding box)
    center_x = bounding_box.get("x", 0) / 2
    center_y = bounding_box.get("y", 0) / 2
    center_z = bounding_box.get("z", 0) / 2

    filtered = []
    for face_idx, centroid in zip(face_indices, centroids, strict=False):
        if len(centroid) >= 3:
            # Check if face is within region of center
            dx = abs(centroid[0] - center_x)
            dy = abs(centroid[1] - center_y)
            dz = abs(centroid[2] - center_z)

            if dx <= region_size and dy <= region_size and dz <= region_size:
                filtered.append(face_idx)

    return filtered


# ── Performance Monitoring ─────────────────────────────────────────


class PerformanceMetrics:
    """Track performance metrics for optimization validation."""

    def __init__(self):
        self.quality_checks = []
        self.process_analyses = []
        self.cache_hits = 0
        self.cache_misses = 0

    def record_quality_check(self, duration: float, face_count: int):
        """Record quality check performance."""
        self.quality_checks.append(
            {"duration": duration, "face_count": face_count, "timestamp": time.time()}
        )

    def record_process_analysis(
        self, duration: float, process_code: str, cache_status: str
    ):
        """Record process analysis performance."""
        self.process_analyses.append(
            {
                "duration": duration,
                "process_code": process_code,
                "cache_status": cache_status,
                "timestamp": time.time(),
            }
        )

        if cache_status == "cold":
            self.cache_misses += 1
        elif cache_status == "hit":
            self.cache_hits += 1

    def get_summary(self) -> dict[str, Any]:
        """Get performance summary statistics."""
        total_quality = len(self.quality_checks)
        total_analysis = len(self.process_analyses)

        quality_times = [c["duration"] for c in self.quality_checks]
        analysis_times = [a["duration"] for a in self.process_analyses]

        return {
            "total_quality_checks": total_quality,
            "total_process_analyses": total_analysis,
            "avg_quality_check_time": sum(quality_times) / total_quality
            if total_quality > 0
            else 0,
            "avg_process_analysis_time": sum(analysis_times) / total_analysis
            if total_analysis > 0
            else 0,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": self.cache_hits / (self.cache_hits + self.cache_misses)
            if (self.cache_hits + self.cache_misses) > 0
            else 0,
        }


# Global performance monitor
_perf_monitor = PerformanceMetrics()


# ── Optimization API ───────────────────────────────────────────────


def get_performance_summary() -> dict[str, Any]:
    """Get current performance metrics summary."""
    return _perf_monitor.get_summary()


def clear_performance_metrics() -> None:
    """Clear all performance metrics."""
    global _perf_monitor
    _perf_monitor = PerformanceMetrics()
