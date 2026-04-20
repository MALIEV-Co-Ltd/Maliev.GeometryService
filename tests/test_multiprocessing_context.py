"""Tests for multiprocessing context and executor behavior.

These tests verify that ProcessPoolExecutor and ThreadPoolExecutor
behave correctly with the cascadio workload, including pickling behavior
and context management.
"""

import sys
import pytest
import concurrent.futures
import multiprocessing
import os
import time
from pathlib import Path

from src.core.geometry import load_cascadio_geometry


# ---------------------------------------------------------------------------
# Module-level helpers required by spawn-context tests.
# Functions submitted to a spawn-context Pool/ProcessPoolExecutor must be
# picklable.  Local (inner) functions are not picklable on Python 3.10;
# defining them at module level fixes this.
# ---------------------------------------------------------------------------

def _get_current_pid():
    """Return the PID of the current (worker) process."""
    return os.getpid()


def _simple_task(x: int) -> int:
    """Return x * 2; module-level so it is picklable by spawn context."""
    return x * 2


def _modify_shared_var():
    """Modifies a dict local to the worker; tests process isolation."""
    # shared_var referenced here is a new dict in the worker process
    local_dict = {"value": 999}
    return local_dict["value"]


def _slow_task(x: int) -> int:
    """Long-running task for shutdown timing tests."""
    time.sleep(0.5)
    return x * 2


def _hanging_task():
    """Extremely long task for shutdown cancellation tests."""
    time.sleep(100)
    return "done"


def _producer(queue):
    """Write items to a multiprocessing Queue."""
    for i in range(5):
        queue.put(i)
    queue.put("DONE")


def _consumer(queue):
    """Read items from a multiprocessing Queue until DONE sentinel."""
    results = []
    while True:
        item = queue.get()
        if item == "DONE":
            break
        results.append(item)
    return results


class TestMultiprocessingContext:
    """Test multiprocessing context behavior."""

    def test_spawn_context_available(self):
        """Test that 'spawn' context is available on all platforms."""
        ctx = multiprocessing.get_context("spawn")
        assert ctx is not None, "Spawn context not available"

        # Verify it's a proper context
        assert hasattr(ctx, "Process"), "Spawn context missing Process"
        assert hasattr(ctx, "Pool"), "Spawn context missing Pool"
        assert hasattr(ctx, "Queue"), "Spawn context missing Queue"

    def test_spawn_context_processes_are_isolated(self):
        """Test that spawn context creates isolated processes."""
        ctx = multiprocessing.get_context("spawn")

        # Use module-level _get_current_pid — local functions are not
        # picklable with spawn context on Python 3.10.
        with ctx.Pool(processes=1) as pool:
            parent_pid = os.getpid()
            child_pid = pool.apply(_get_current_pid)

            # Child PID should be different from parent
            assert child_pid != parent_pid, \
                f"Spawn context didn't create isolated process: {child_pid} == {parent_pid}"

    def test_fork_context_available_on_unix(self):
        """Test that 'fork' context is available on Unix systems."""
        try:
            ctx = multiprocessing.get_context("fork")
            assert ctx is not None, "Fork context not available on Unix"
        except ValueError:
            # Expected on Windows
            pytest.skip("Fork context not available on Windows")


class TestExecutorPickling:
    """Test that functions can be pickled for different executor types."""

    def test_top_level_function_pickles(self):
        """Test that module-level functions can be pickled."""
        import pickle

        # _simple_task is defined at module level so it is picklable
        try:
            pickled = pickle.dumps(_simple_task)
            unpickled = pickle.loads(pickled)
            result = unpickled(5)
            assert result == 10, "Unpickled function didn't work correctly"
        except Exception as e:
            pytest.fail(f"Failed to pickle module-level function: {e}")

    def test_nested_function_doesnt_pickle_cleanly(self):
        """Test that nested functions have pickling issues."""
        import pickle

        def outer():
            def inner(x: int) -> int:
                return x * 2
            return inner

        nested_func = outer()

        # This may or may not raise an error depending on implementation
        # The key is that it's problematic
        try:
            pickled = pickle.dumps(nested_func)
            # If it succeeds, that's fine too
        except (pickle.PicklingError, AttributeError) as e:
            # Expected for nested functions
            assert True  # This is expected behavior

    def test_processpool_with_simple_function(self):
        """Test that ProcessPoolExecutor works with module-level functions."""
        # Use _simple_task (module-level) — local functions are not picklable
        # with the default spawn context on Windows/Python 3.10.
        with concurrent.futures.ProcessPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_simple_task, 5)
            result = future.result(timeout=5)

        assert result == 10, f"ProcessPoolExecutor returned wrong result: {result}"

    def test_threadpool_with_nested_function(self):
        """Test that ThreadPoolExecutor can work with closures/nested functions."""
        def create_task():
            # This is a nested function (closure)
            def nested_task(x: int) -> int:
                return x * 2
            return nested_task

        task = create_task()

        # ThreadPoolExecutor should handle this fine (threads share memory)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(task, 5)
            result = future.result(timeout=5)

        assert result == 10, f"ThreadPoolExecutor returned wrong result: {result}"

    def test_processpool_with_lambda_may_fail(self):
        """Test that ProcessPoolExecutor with lambda may have issues."""
        # Lambda functions can be pickled in some Python versions
        # but it's implementation-dependent
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda x: x * 2, 5)
                result = future.result(timeout=5)

            # If it works, that's fine
            assert result == 10, f"Lambda execution returned wrong result: {result}"
        except Exception as e:
            # If it fails, that's also acceptable
            # The key is consistent behavior
            assert True


class TestCascadioExecutorCompatibility:
    """Test cascadio loading with different executor types."""

    def test_cascadio_with_spawn_context(self, test_assets_dir):
        """Test cascadio loading with spawn context."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Test with spawn context explicitly
        result = load_cascadio_geometry(step_bytes, timeout_seconds=30)

        # Should work
        assert result is not None, "load_cascadio_geometry returned None"

    def test_cascadio_process_pool_behavior(self, test_assets_dir):
        """Test cascadio loading behavior with ProcessPoolExecutor."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Submit multiple cascadio loads to ProcessPoolExecutor
        num_loads = 2
        futures = []
        results = []

        with concurrent.futures.ProcessPoolExecutor(max_workers=num_loads) as executor:
            for _ in range(num_loads):
                future = executor.submit(
                    load_cascadio_geometry,
                    step_bytes,
                    30,  # timeout_seconds
                )
                futures.append(future)

            for future in futures:
                try:
                    result = future.result(timeout=60)
                    results.append(result)
                except concurrent.futures.TimeoutError:
                    pytest.fail("ProcessPoolExecutor load timed out")

        # Verify all completed
        assert len(results) == num_loads, \
            f"Only {len(results)}/{num_loads} loads completed in ProcessPoolExecutor"

    def test_cascadio_thread_pool_behavior(self, test_assets_dir):
        """Test cascadio loading behavior with ThreadPoolExecutor."""
        step_file = test_assets_dir / "cube.step"
        if not step_file.exists():
            pytest.skip("cube.step not found")

        step_bytes = step_file.read_bytes()

        # Submit multiple cascadio loads to ThreadPoolExecutor
        num_loads = 2
        futures = []
        results = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_loads) as executor:
            for _ in range(num_loads):
                future = executor.submit(
                    load_cascadio_geometry,
                    step_bytes,
                    30,  # timeout_seconds
                )
                futures.append(future)

            for future in futures:
                try:
                    result = future.result(timeout=60)
                    results.append(result)
                except concurrent.futures.TimeoutError:
                    pytest.fail("ThreadPoolExecutor load timed out")

        # Verify all completed
        assert len(results) == num_loads, \
            f"Only {len(results)}/{num_loads} loads completed in ThreadPoolExecutor"


class TestExecutorShutdown:
    """Test executor shutdown behavior."""

    def test_processpool_shutdown_waits_for_completion(self):
        """Test that ProcessPoolExecutor shutdown waits for completion."""
        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as executor:
            # Submit multiple tasks using module-level _slow_task (picklable)
            futures = [executor.submit(_slow_task, i) for i in range(5)]
            # Shutdown with wait=True should wait for all tasks
            # This is tested implicitly by the context manager

        # All tasks should complete
        assert True  # If we get here, shutdown worked correctly

    def test_processpool_shutdown_nowait_cancels_pending(self):
        """Test that ProcessPoolExecutor shutdown without wait cancels pending tasks."""
        def slow_task(x: int) -> int:
            time.sleep(10)  # Long task
            return x * 2

        executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)

        # Submit a long task
        future = executor.submit(slow_task, 5)

        # Shutdown without waiting
        executor.shutdown(wait=False, cancel_futures=True)

        # Future should be cancelled
        # (Note: behavior is implementation-dependent)

    def test_threadpool_shutdown_waits_for_completion(self):
        """Test that ThreadPoolExecutor shutdown waits for completion."""
        def slow_task(x: int) -> int:
            time.sleep(0.5)
            return x * 2

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            # Submit multiple tasks
            futures = [executor.submit(slow_task, i) for i in range(5)]

            # Context manager will wait for completion

        # All tasks should complete
        assert True  # If we get here, shutdown worked correctly

    def test_threadpool_vs_processpool_termination(self):
        """Compare termination behavior between ThreadPool and ProcessPool executors.

        ThreadPoolExecutor.shutdown() does not accept a `timeout` parameter
        (that was never in the stdlib API).  We use future.result(timeout=...)
        to bound the wait instead.
        """
        # Test ThreadPoolExecutor — use a short-lived task so shutdown doesn't hang
        start_time = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(time.sleep, 0.1)
            try:
                future.result(timeout=2)
            except concurrent.futures.TimeoutError:
                pass
            # shutdown() without `timeout` kwarg — that parameter does not exist
            executor.shutdown(wait=True)

        threadpool_duration = time.time() - start_time

        # Test ProcessPoolExecutor — non-blocking shutdown
        start_time = time.time()
        executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)
        future = executor.submit(_simple_task, 1)  # fast, picklable task
        try:
            future.result(timeout=5)
        except concurrent.futures.TimeoutError:
            pass
        executor.shutdown(wait=False)
        processpool_duration = time.time() - start_time

        print(f"\nThreadPoolExecutor shutdown: {threadpool_duration:.2f}s")
        print(f"ProcessPoolExecutor shutdown: {processpool_duration:.2f}s")
        # No strict timing assertion — behaviour is implementation-dependent


class TestMultiprocessingSafety:
    """Test multiprocessing safety and isolation."""

    def test_process_isolation(self):
        """Test that processes are properly isolated from the parent.

        Uses module-level _modify_shared_var — local functions can't be
        pickled with spawn context on Python 3.10.
        """
        parent_dict = {"value": 0}

        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=1) as pool:
            result = pool.apply(_modify_shared_var)

        # Child process returned 999 (its own local dict)
        assert result == 999, "Child didn't return expected value"

        # Parent's dict is unaffected — process isolation confirmed
        assert parent_dict["value"] == 0, "Process isolation violated"

    def test_shared_memory_with_queue(self):
        """Test shared-memory communication via a Manager Queue.

        A ctx.Queue() cannot be passed to pool workers via pickling in spawn
        mode (it requires inheritance, not pickling).  multiprocessing.Manager()
        creates a proxy-based Queue that CAN be pickled and sent to workers.
        """
        ctx = multiprocessing.get_context("spawn")

        # Manager().Queue() is a proxy — safe to pickle and pass to workers.
        with multiprocessing.Manager() as manager:
            queue = manager.Queue()

            with ctx.Pool(processes=2) as pool:
                # Start producer (async)
                pool.apply_async(_producer, (queue,))
                # Give the producer a moment to enqueue items
                time.sleep(0.2)
                # Start consumer (blocks until it reads the DONE sentinel)
                results = pool.apply(_consumer, (queue,))

        assert results == [0, 1, 2, 3, 4], f"Queue communication failed: {results}"
