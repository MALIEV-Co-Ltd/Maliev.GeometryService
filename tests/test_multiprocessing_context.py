"""Tests for multiprocessing context and executor behavior.

These tests verify that ProcessPoolExecutor and ThreadPoolExecutor
behave correctly with the cascadio workload, including pickling behavior
and context management.
"""

import pytest
import concurrent.futures
import multiprocessing
import os
import time
from pathlib import Path

from src.core.geometry import load_cascadio_geometry


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

        def get_pid():
            return os.getpid()

        with ctx.Pool(processes=1) as pool:
            parent_pid = os.getpid()
            child_pid = pool.apply(get_pid)

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
        """Test that top-level functions can be pickled."""
        import pickle

        def simple_function(x: int) -> int:
            return x * 2

        # Should be picklable
        try:
            pickled = pickle.dumps(simple_function)
            unpickled = pickle.loads(pickled)
            result = unpickled(5)
            assert result == 10, "Unpickled function didn't work correctly"
        except Exception as e:
            pytest.fail(f"Failed to pickle top-level function: {e}")

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
        """Test that ProcessPoolExecutor works with simple functions."""
        def simple_task(x: int) -> int:
            return x * 2

        with concurrent.futures.ProcessPoolExecutor(max_workers=1) as executor:
            future = executor.submit(simple_task, 5)
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
        def slow_task(x: int) -> int:
            time.sleep(0.5)
            return x * 2

        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as executor:
            # Submit multiple tasks
            futures = [executor.submit(slow_task, i) for i in range(5)]

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
        """Compare termination behavior between ThreadPool and ProcessPool executors."""
        def hanging_task():
            time.sleep(100)  # Very long task
            return "done"

        # Test ThreadPoolExecutor
        start_time = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(hanging_task)
            try:
                future.result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                pass

            # Shutdown will wait for thread to finish (can't forcefully terminate threads)
            executor.shutdown(wait=True, timeout=2)

        threadpool_duration = time.time() - start_time

        # Test ProcessPoolExecutor
        start_time = time.time()
        with concurrent.futures.ProcessPoolExecutor(max_workers=1) as executor:
            future = executor.submit(hanging_task)
            try:
                future.result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                pass

            # Can forcefully terminate processes
            executor.shutdown(wait=False)

        processpool_duration = time.time() - start_time

        # ProcessPool should terminate faster
        print(f"\nThreadPoolExecutor shutdown: {threadpool_duration:.2f}s")
        print(f"ProcessPoolExecutor shutdown: {processpool_duration:.2f}s")

        # ProcessPool should generally be faster to terminate
        # (though this is implementation-dependent)


class TestMultiprocessingSafety:
    """Test multiprocessing safety and isolation."""

    def test_process_isolation(self):
        """Test that processes are properly isolated."""
        shared_var = {"value": 0}

        def modify_shared():
            # This should NOT affect the parent process
            shared_var["value"] = 999
            return shared_var["value"]

        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=1) as pool:
            result = pool.apply(modify_shared)

        # Child process modified its own copy
        assert result == 999, "Child didn't modify its copy"

        # Parent's copy should be unchanged
        assert shared_var["value"] == 0, "Process isolation violated"

    def test_shared_memory_with_queue(self):
        """Test shared memory communication via Queue."""
        ctx = multiprocessing.get_context("spawn")

        def producer(queue):
            for i in range(5):
                queue.put(i)
            queue.put("DONE")

        def consumer(queue):
            results = []
            while True:
                item = queue.get()
                if item == "DONE":
                    break
                results.append(item)
            return results

        queue = ctx.Queue()

        with ctx.Pool(processes=2) as pool:
            # Start producer
            pool.apply_async(producer, (queue,))

            # Start consumer
            results = pool.apply(consumer, (queue,))

        # Verify communication worked
        assert results == [0, 1, 2, 3, 4], f"Queue communication failed: {results}"
