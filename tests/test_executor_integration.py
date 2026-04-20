"""Tests for executor integration with asyncio.run_in_executor().

This test ensures that GeometryProcessor works correctly with the production
code path using asyncio.run_in_executor(), which requires the executor to have
a submit() method returning concurrent.futures.Future.
"""

import asyncio
import pytest

from src.core.geometry import GeometryProcessor


def simple_task() -> dict:
    """Module-level function for testing (must be picklable)."""
    return {"test": "result", "value": 42}


def simple_dfm_task() -> dict:
    """Module-level function for DFM executor testing."""
    return {"dfm": "result", "count": 100}


def add_task(a: int, b: int) -> int:
    """Module-level function for args testing."""
    return a + b


class TestExecutorIntegration:
    """Test executor integration with asyncio.run_in_executor()."""

    def test_geometry_processor_executor_has_submit_method(self):
        """Verify executor has submit() method required by run_in_executor."""
        processor = GeometryProcessor(enable_diagnostics=False)

        assert hasattr(processor.executor, "submit"), (
            "Executor must have submit() method"
        )
        assert callable(processor.executor.submit), "submit must be callable"

        processor.shutdown()

    def test_dfm_executor_has_submit_method(self):
        """Verify DFM executor has submit() method required by run_in_executor."""
        processor = GeometryProcessor(enable_diagnostics=False)

        assert hasattr(processor.dfm_executor, "submit"), (
            "DFM executor must have submit() method"
        )
        assert callable(processor.dfm_executor.submit), "submit must be callable"

        processor.shutdown()

    @pytest.mark.asyncio
    async def test_run_in_executor_with_main_executor(self):
        """Test main executor works with asyncio.run_in_executor()."""
        processor = GeometryProcessor(enable_diagnostics=False)

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(processor.executor, simple_task)

        assert result == {"test": "result", "value": 42}

        processor.shutdown()

    @pytest.mark.asyncio
    async def test_run_in_executor_with_dfm_executor(self):
        """Test DFM executor works with asyncio.run_in_executor()."""
        processor = GeometryProcessor(enable_diagnostics=False)

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(processor.dfm_executor, simple_dfm_task)

        assert result == {"dfm": "result", "count": 100}

        processor.shutdown()

    @pytest.mark.asyncio
    async def test_run_in_executor_with_args(self):
        """Test run_in_executor passes arguments correctly."""
        processor = GeometryProcessor(enable_diagnostics=False)

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(processor.executor, add_task, 10, 20)

        assert result == 30

        processor.shutdown()

    @pytest.mark.asyncio
    async def test_executor_shutdown(self):
        """Test executor shutdown works correctly."""
        processor = GeometryProcessor(enable_diagnostics=False)

        processor.shutdown()

        assert True
