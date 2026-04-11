"""Test utility functions for timeout testing, process monitoring, and memory tracking."""

import os
import sys
import time
import signal
import threading
import psutil
import logging
from typing import Optional, Callable, Any, Dict, List
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProcessSnapshot:
    """Snapshot of process state at a point in time."""
    timestamp: float
    pid: int
    rss_mb: float
    vms_mb: float
    num_threads: int
    open_files: int
    children_count: int
    temp_file_count: int


class ProcessMonitor:
    """Monitor process state during test execution."""

    def __init__(self, pid: Optional[int] = None, poll_interval: float = 0.5):
        """Initialize process monitor.

        Args:
            pid: Process ID to monitor (defaults to current process)
            poll_interval: Seconds between snapshots
        """
        self.pid = pid or os.getpid()
        self.poll_interval = poll_interval
        self.snapshots: List[ProcessSnapshot] = []
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start monitoring process state."""
        if self._monitoring:
            return

        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info(f"Started monitoring process {self.pid}")

    def stop(self) -> None:
        """Stop monitoring process state."""
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None
        logger.info(f"Stopped monitoring process {self.pid}, collected {len(self.snapshots)} snapshots")

    def _monitor_loop(self) -> None:
        """Monitoring loop that runs in background thread."""
        while self._monitoring:
            try:
                snapshot = self._take_snapshot()
                self.snapshots.append(snapshot)
            except Exception as e:
                logger.warning(f"Failed to take snapshot: {e}")
            time.sleep(self.poll_interval)

    def _take_snapshot(self) -> ProcessSnapshot:
        """Take a snapshot of current process state."""
        try:
            process = psutil.Process(self.pid)
            return ProcessSnapshot(
                timestamp=time.time(),
                pid=self.pid,
                rss_mb=process.memory_info().rss / 1024 / 1024,
                vms_mb=process.memory_info().vms / 1024 / 1024,
                num_threads=process.num_threads(),
                open_files=len(process.open_files()),
                children_count=len(process.children(recursive=False)),
                temp_file_count=_count_temp_files(),
            )
        except Exception as e:
            logger.warning(f"Error taking snapshot: {e}")
            return ProcessSnapshot(
                timestamp=time.time(),
                pid=self.pid,
                rss_mb=0.0,
                vms_mb=0.0,
                num_threads=0,
                open_files=0,
                children_count=0,
                temp_file_count=_count_temp_files(),
            )

    def get_peak_memory(self) -> float:
        """Get peak RSS memory usage in MB."""
        return max((s.rss_mb for s in self.snapshots), default=0.0)

    def get_final_memory(self) -> float:
        """Get final RSS memory usage in MB."""
        return self.snapshots[-1].rss_mb if self.snapshots else 0.0

    def get_memory_growth(self) -> float:
        """Get memory growth in MB (final - initial)."""
        if len(self.snapshots) < 2:
            return 0.0
        return self.snapshots[-1].rss_mb - self.snapshots[0].rss_mb

    def has_orphaned_children(self) -> bool:
        """Check if process has orphaned children."""
        if not self.snapshots:
            return False
        # Check if any snapshot shows children
        return any(s.children_count > 0 for s in self.snapshots[-5:])  # Last 5 snapshots

    def get_temp_file_growth(self) -> int:
        """Get temp file count change."""
        if len(self.snapshots) < 2:
            return 0
        return self.snapshots[-1].temp_file_count - self.snapshots[0].temp_file_count


def _count_temp_files() -> int:
    """Count temporary files in /tmp directory."""
    try:
        import tempfile
        from pathlib import Path

        temp_dir = tempfile.gettempdir()
        return len([f for f in Path(temp_dir).iterdir() if f.is_file()])
    except Exception:
        return 0


class TimeoutTester:
    """Helper for testing timeout behavior."""

    def __init__(self, timeout_seconds: float):
        """Initialize timeout tester.

        Args:
            timeout_seconds: Timeout duration in seconds
        """
        self.timeout_seconds = timeout_seconds
        self.timed_out = False
        self.completed = False
        self._alarm_set = False

    def __enter__(self):
        """Enter context manager, set timeout alarm."""
        if sys.platform != "win32":
            # Unix-like systems - use SIGALRM
            signal.signal(signal.SIGALRM, self._timeout_handler)
            signal.alarm(int(self.timeout_seconds))
            self._alarm_set = True
        else:
            # Windows - use watchdog thread
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                daemon=True,
            )
            self._watchdog_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager, cancel timeout."""
        if sys.platform != "win32" and self._alarm_set:
            signal.alarm(0)
            self._alarm_set = False
        return False

    def _timeout_handler(self, signum, frame):
        """Handle timeout signal on Unix."""
        self.timed_out = True
        raise TimeoutError(f"Operation timed out after {self.timeout_seconds}s")

    def _watchdog_loop(self):
        """Watchdog loop for Windows timeout."""
        end_time = time.time() + self.timeout_seconds
        while time.time() < end_time and not self.completed:
            time.sleep(0.1)
        if not self.completed:
            self.timed_out = True
            # Can't raise exception from thread, set flag instead

    def mark_completed(self):
        """Mark operation as completed."""
        self.completed = True


@contextmanager
def monitor_resources():
    """Context manager to monitor process resources during execution.

    Yields:
        ProcessMonitor instance
    """
    monitor = ProcessMonitor()
    monitor.start()
    try:
        yield monitor
    finally:
        monitor.stop()


def check_for_orphaned_processes(expected_parent_pid: Optional[int] = None) -> List[Dict[str, Any]]:
    """Check for orphaned processes related to geometry processing.

    Args:
        expected_parent_pid: Expected parent PID (defaults to current process)

    Returns:
        List of orphaned process info dicts
    """
    parent_pid = expected_parent_pid or os.getpid()
    orphans = []

    try:
        current_process = psutil.Process(parent_pid)
        all_children = current_process.children(recursive=True)

        for child in all_children:
            try:
                # Check if it's a geometry worker process
                cmd = " ".join(child.cmdline())
                if any(keyword in cmd.lower() for keyword in ["gmsh", "cascadio", "python"]):
                    orphans.append({
                        "pid": child.pid,
                        "name": child.name(),
                        "cmdline": cmd,
                        "rss_mb": child.memory_info().rss / 1024 / 1024,
                        "create_time": child.create_time(),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except psutil.NoSuchProcess:
        pass

    return orphans


def cleanup_temp_files(prefix: str = "geom_") -> int:
    """Clean up temporary files matching a prefix.

    Args:
        prefix: File prefix to match

    Returns:
        Number of files cleaned up
    """
    import tempfile
    from pathlib import Path

    temp_dir = tempfile.gettempdir()
    cleaned = 0

    try:
        for f in Path(temp_dir).iterdir():
            if f.is_file() and f.name.startswith(prefix):
                try:
                    f.unlink()
                    cleaned += 1
                except Exception:
                    pass
    except Exception:
        pass

    return cleaned


def wait_for_condition(
    condition: Callable[[], bool],
    timeout_seconds: float = 30.0,
    poll_interval: float = 0.1,
    error_message: str = "Condition not met",
) -> None:
    """Wait for a condition to become true.

    Args:
        condition: Callable that returns True when condition is met
        timeout_seconds: Maximum time to wait
        poll_interval: Time between checks
        error_message: Error message if timeout occurs

    Raises:
        TimeoutError: If condition is not met within timeout
    """
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        if condition():
            return
        time.sleep(poll_interval)
    raise TimeoutError(f"{error_message} (waited {timeout_seconds}s)")


def measure_performance(func: Callable[[], Any]) -> Dict[str, Any]:
    """Measure execution time and memory usage of a function.

    Args:
        func: Function to measure

    Returns:
        Dict with 'duration_seconds', 'rss_mb_start', 'rss_mb_end', 'rss_mb_delta'
    """
    process = psutil.Process()
    rss_start = process.memory_info().rss / 1024 / 1024

    start_time = time.time()
    try:
        func()
        success = True
        error = None
    except Exception as e:
        success = False
        error = str(e)

    duration = time.time() - start_time
    rss_end = process.memory_info().rss / 1024 / 1024

    return {
        "duration_seconds": duration,
        "rss_mb_start": rss_start,
        "rss_mb_end": rss_end,
        "rss_mb_delta": rss_end - rss_start,
        "success": success,
        "error": error,
    }


class MockCascadioServer:
    """Mock cascadio server for testing without actual CAD conversions."""

    def __init__(self, delay_seconds: float = 0.0, should_timeout: bool = False):
        """Initialize mock server.

        Args:
            delay_seconds: Artificial delay before responding
            should_timeout: If True, never respond (simulates timeout)
        """
        self.delay_seconds = delay_seconds
        self.should_timeout = should_timeout
        self.requests_received = []

    def load_step(self, step_bytes: bytes) -> Dict[str, Any]:
        """Mock STEP file loading."""
        self.requests.append({
            "type": "load_step",
            "size": len(step_bytes),
            "timestamp": time.time(),
        })

        if self.should_timeout:
            # Simulate timeout by sleeping forever
            time.sleep(1000)

        time.sleep(self.delay_seconds)

        # Return mock result
        return {
            "success": True,
            "bodies": [
                {
                    "name": "MockBody1",
                    "mesh": self._generate_mock_mesh(),
                }
            ],
        }

    def _generate_mock_mesh(self) -> bytes:
        """Generate mock mesh data."""
        # Simple mock GLB data
        return b"glTF mock data"
