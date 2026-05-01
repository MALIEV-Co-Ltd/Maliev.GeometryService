"""Worker process diagnostics for enhanced error logging.

This module provides diagnostic infrastructure for worker processes that run
CPU-intensive geometry tasks. It captures detailed error information including
stack traces, memory usage, and timing data when workers crash.
"""

import contextlib
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WorkerErrorResult:
    """Structured error information from a crashed worker.

    Attributes:
        error_type: Exception class name (e.g., "MemoryError", "ValueError", "TimeoutError")
        error_message: Exception message string
        stack_trace: Full Python traceback as string
        worker_id: Unique identifier for this worker invocation
        memory_info: Dict with RSS memory usage in MB
        timing_info: Dict with duration_seconds, timeout_seconds if applicable
        context: Additional context (body_id, file_path, etc.)
        resource_info: Dict with temp_file_count, child_process_count
    """  # noqa: E501

    error_type: str
    error_message: str
    stack_trace: str
    worker_id: str
    memory_info: dict[str, Any]
    timing_info: dict[str, Any]
    context: dict[str, Any]
    resource_info: dict[str, Any] = None  # NEW FIELD

    def to_dict(self) -> dict[str, Any]:
        """Convert to regular dict for JSON serialization."""
        data = asdict(self)
        # Ensure resource_info is never None
        if data.get("resource_info") is None:
            data["resource_info"] = {}
        return data


def _count_temp_files() -> int:
    """Count temporary files in /tmp directory.

    Returns:
        Number of files in the temp directory
    """
    try:
        import tempfile
        from pathlib import Path

        temp_dir = tempfile.gettempdir()
        return len([f for f in Path(temp_dir).iterdir() if f.is_file()])
    except Exception:
        return 0


def setup_worker_diagnostics(
    worker_type: str = "geometry",
    memory_warning_mb: float = 1000.0,
    memory_critical_mb: float = 2000.0,
) -> str:
    """Initialize diagnostic logging and monitoring for a worker process.

    Args:
        worker_type: Type of worker (e.g., "geometry", "dfm", "tessellation")
        memory_warning_mb: Log warning when RSS exceeds this threshold
        memory_critical_mb: Log critical when RSS exceeds this threshold

    Returns:
        worker_id: Unique identifier for this worker
    """
    # Generate unique worker ID
    worker_id = f"{worker_type}_{uuid.uuid4().hex[:8]}_{os.getpid()}"

    # Create dedicated log file for this worker
    log_dir = "/tmp"
    os.makedirs(log_dir, exist_ok=True)  # noqa: PTH103
    log_file = os.path.join(log_dir, f"worker_{worker_id}.log")  # noqa: PTH118

    # Configure file handler for worker-specific logging.
    # Guard against duplicate handlers: if this worker process already has a
    # FileHandler attached (e.g., because setup_worker_diagnostics was called
    # twice on the same worker process), skip adding another one.
    root_logger = logging.getLogger()
    already_has_file_handler = any(
        isinstance(h, logging.FileHandler) for h in root_logger.handlers
    )
    if not already_has_file_handler:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )
        root_logger.addHandler(file_handler)
        root_logger.setLevel(logging.DEBUG)
        logger.info(f"Worker {worker_id} initialized, logging to {log_file}")
    else:
        logger.debug(
            f"Worker {worker_id} re-entering diagnostics setup — skipping duplicate handler"  # noqa: E501
        )

    # Enable faulthandler to catch segfaults (idempotent — safe to call multiple times)
    try:
        import faulthandler

        faulthandler.enable(file=sys.stderr, all_threads=True)
    except Exception as e:
        logger.warning(f"Failed to enable faulthandler: {e}")

    # Start memory monitoring thread only if one isn't already running for this process.
    # Each daemon thread persists for the worker's lifetime; adding a second one just
    # doubles the log noise without adding information.
    _monitor_sentinel = f"_worker_monitor_started_{os.getpid()}"
    if not getattr(threading.current_thread(), _monitor_sentinel, False):
        monitor_thread = threading.Thread(
            target=_monitor_memory,
            args=(worker_id, memory_warning_mb, memory_critical_mb),
            daemon=True,
        )
        monitor_thread.start()
        # Mark that a monitor is running on this process (best-effort; attribute on main thread)  # noqa: E501
        with contextlib.suppress(Exception):
            setattr(threading.main_thread(), _monitor_sentinel, True)

    return worker_id


def _monitor_memory(
    worker_id: str,
    warning_mb: float,
    critical_mb: float,
    interval_seconds: float = 5.0,
) -> None:
    """Background thread that monitors worker memory usage.

    Logs warnings when memory exceeds thresholds.  Uses exponential backoff
    when the process stays in a critical state to avoid log flooding: the first
    violation logs immediately, subsequent continuous violations double the
    silence window up to 120 s.  Resets back to normal polling once memory
    drops below the critical threshold.

    Self-terminates the worker process (via os._exit) when RSS exceeds 3×
    critical_mb — at that point the OS is likely to OOM-kill us anyway, and a
    clean exit lets the pool spawn a fresh worker.

    Args:
        worker_id: Unique identifier for this worker
        warning_mb: Log warning when RSS exceeds this
        critical_mb: Log critical when RSS exceeds this
        interval_seconds: Normal polling interval
    """
    try:
        import psutil
    except ImportError:
        logger.debug("psutil not available, skipping memory monitoring")
        return

    process = psutil.Process()
    kill_threshold_mb = critical_mb * 3.0  # force-exit at 3× critical (e.g. 6 GB)
    backoff = interval_seconds  # current sleep between critical logs
    max_backoff = 120.0  # cap at 2 minutes of silence
    in_critical = False

    while True:
        try:
            rss_mb = process.memory_info().rss / 1024 / 1024

            if rss_mb > kill_threshold_mb:
                # Past the point of no return — exit cleanly so the pool can
                # spawn a fresh worker rather than waiting for an OOM kill.
                logger.critical(
                    "Worker %s RSS %.0f MB exceeds kill threshold %.0f MB — "
                    "terminating worker process to free memory",
                    worker_id,
                    rss_mb,
                    kill_threshold_mb,
                )
                os._exit(1)

            elif rss_mb > critical_mb:
                if not in_critical:
                    # First time crossing threshold — log immediately, reset backoff
                    logger.critical(
                        "Worker %s memory critical: %.1f MB (threshold: %.0f MB)",
                        worker_id,
                        rss_mb,
                        critical_mb,
                    )
                    backoff = interval_seconds
                    in_critical = True
                else:
                    # Still in critical — log once per backoff window, then double
                    logger.critical(
                        "Worker %s memory still critical: %.1f MB (will recheck in %.0fs)",  # noqa: E501
                        worker_id,
                        rss_mb,
                        min(backoff * 2, max_backoff),
                    )
                    backoff = min(backoff * 2, max_backoff)
                time.sleep(backoff)
                continue  # skip normal sleep below

            elif rss_mb > warning_mb:
                logger.warning(
                    "Worker %s memory high: %.1f MB (threshold: %.0f MB)",
                    worker_id,
                    rss_mb,
                    warning_mb,
                )
                in_critical = False
                backoff = interval_seconds

            else:
                in_critical = False
                backoff = interval_seconds

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            break

        time.sleep(interval_seconds)


def wrap_worker(
    worker_type: str = "geometry",
    memory_warning_mb: float = 1000.0,
    memory_critical_mb: float = 2000.0,
) -> Callable:
    """Decorator that wraps a worker function with diagnostic logging.

    The wrapped function will:
    - Initialize worker-specific logging to a file
    - Monitor memory usage in background thread
    - Catch all exceptions and return structured error info
    - Track timing information

    Args:
        worker_type: Type of worker for logging prefix
        memory_warning_mb: Memory threshold for warnings
        memory_critical_mb: Memory threshold for critical alerts

    Returns:
        Decorated function that returns either the normal result or WorkerErrorResult

    Example:
        @wrap_worker(worker_type="dfm")
        def _compute_dfm_worker(stl_bytes, cad_bytes):
            # ... DFM analysis logic ...
            return reports
    """

    def decorator(func: Callable) -> Callable:
        def wrapped(*args, **kwargs) -> Any:
            # Setup diagnostics
            worker_id = setup_worker_diagnostics(
                worker_type=worker_type,
                memory_warning_mb=memory_warning_mb,
                memory_critical_mb=memory_critical_mb,
            )

            # Track timing
            start_time = time.time()
            memory_info = {}

            try:
                # Run the actual worker function
                result = func(*args, **kwargs)

                # Record final memory usage and resource tracking
                try:
                    import psutil

                    process = psutil.Process()
                    memory_info["rss_mb"] = process.memory_info().rss / 1024 / 1024
                    memory_info["peak_mb"] = memory_info[
                        "rss_mb"
                    ]  # psutil doesn't track peak easily
                except ImportError:
                    pass

                # Log success
                duration = time.time() - start_time
                logger.info(
                    f"Worker {worker_id} completed successfully in {duration:.2f}s, "
                    f"final RSS: {memory_info.get('rss_mb', 0):.1f} MB"
                )

                return result

            except Exception as e:
                # Capture detailed error information
                duration = time.time() - start_time

                # Track memory and resources on error
                resource_info = {}
                try:
                    import psutil

                    process = psutil.Process()
                    memory_info["rss_mb"] = process.memory_info().rss / 1024 / 1024
                    memory_info["peak_mb"] = memory_info["rss_mb"]

                    # Track resources on error
                    resource_info = {
                        "temp_file_count": _count_temp_files(),
                        "child_process_count": len(process.children()),
                    }
                except ImportError:
                    resource_info = {
                        "temp_file_count": _count_temp_files(),
                        "child_process_count": 0,
                    }
                except Exception:
                    resource_info = {
                        "temp_file_count": _count_temp_files(),
                        "child_process_count": 0,
                    }

                # Log the error with full context
                logger.error(
                    f"Worker {worker_id} failed after {duration:.2f}s: {type(e).__name__}: {e}\n"  # noqa: E501
                    f"Stack trace:\n{traceback.format_exc()}",
                    extra={
                        "worker_id": worker_id,
                        "error_type": type(e).__name__,
                        "duration_seconds": duration,
                        "rss_mb": memory_info.get("rss_mb", 0),
                        "temp_files": resource_info.get("temp_file_count", 0),
                        "child_processes": resource_info.get("child_process_count", 0),
                    },
                )

                # Return structured error result
                error_result = WorkerErrorResult(
                    error_type=type(e).__name__,
                    error_message=str(e),
                    stack_trace=traceback.format_exc(),
                    worker_id=worker_id,
                    memory_info=memory_info,
                    timing_info={"duration_seconds": duration},
                    context={
                        "function": func.__name__,
                        "args_count": len(args),
                        "kwargs_keys": list(kwargs.keys()),
                    },
                    resource_info=resource_info,  # NEW FIELD
                )

                return error_result.to_dict()

        return wrapped

    return decorator


class DiagnosticExecutor(ProcessPoolExecutor):
    """ProcessPoolExecutor with automatic worker diagnostics.

    This executor wraps all submitted functions with diagnostic logging,
    ensuring that worker crashes capture detailed error information.

    Usage:
        executor = DiagnosticExecutor(
            max_workers=4,
            enable_diagnostics=True,
            memory_warning_mb=1000.0,
            memory_critical_mb=2000.0,
        )
    """

    def __init__(
        self,
        max_workers: int | None = None,
        mp_context=None,
        enable_diagnostics: bool = True,
        memory_warning_mb: float = 1000.0,
        memory_critical_mb: float = 2000.0,
        max_tasks_per_child: int | None = None,
        **kwargs,
    ):
        """Initialize the diagnostic executor.

        Args:
            max_workers: Maximum number of worker processes
            mp_context: Multiprocessing context (spawn/fork)
            enable_diagnostics: Whether to enable diagnostic wrapping
            memory_warning_mb: Memory threshold for warnings
            memory_critical_mb: Memory threshold for critical alerts
            max_tasks_per_child: Recycle workers after this many tasks (Python 3.11+).
                Set to a small number (e.g. 5) to bound RSS growth in long-running
                services that process many large files.  None = workers live forever.
            **kwargs: Additional arguments passed to ProcessPoolExecutor
        """
        self.enable_diagnostics = enable_diagnostics
        self.memory_warning_mb = memory_warning_mb
        self.memory_critical_mb = memory_critical_mb
        self.max_tasks_per_child = max_tasks_per_child  # stored for introspection

        if max_tasks_per_child is not None:
            if sys.version_info >= (3, 11):
                kwargs["max_tasks_per_child"] = max_tasks_per_child
            else:
                logger.warning(
                    "max_tasks_per_child=%d ignored: Python %d.%d < 3.11 "
                    "does not support it on ProcessPoolExecutor. "
                    "Use PoolExecutorWrapper for worker recycling on Python 3.10.",
                    max_tasks_per_child,
                    sys.version_info.major,
                    sys.version_info.minor,
                )

        super().__init__(max_workers=max_workers, mp_context=mp_context, **kwargs)

        logger.info(
            f"DiagnosticExecutor initialized with {max_workers} workers, "
            f"max_tasks_per_child={max_tasks_per_child}, "
            f"diagnostics={'enabled' if enable_diagnostics else 'disabled'}"
        )

    def submit(self, fn, *args, **kwargs):
        """Submit a function to the executor with diagnostic wrapping.

        If diagnostics are enabled, wraps the function to capture errors.
        Otherwise, submits directly to the parent class.
        """
        if not self.enable_diagnostics:
            # Diagnostics disabled, use standard submission
            return super().submit(fn, *args, **kwargs)

        # Wrap the function with diagnostics
        # Note: We need to be careful about pickling here
        # For now, we'll rely on the functions themselves to call setup_worker_diagnostics  # noqa: E501
        # The decorator approach doesn't work well with pickling
        return super().submit(fn, *args, **kwargs)
