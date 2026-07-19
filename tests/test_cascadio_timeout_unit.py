"""Unit coverage for cascadio subprocess timeout cleanup."""

from __future__ import annotations

import multiprocessing

import pytest


class _TimeoutFuture:
    def get(self, timeout: float | int | None = None):  # noqa: ARG002
        raise multiprocessing.TimeoutError


class _TimeoutPool:
    def __init__(self):
        self.closed = False
        self.join_count = 0
        self.terminated = False

    def apply_async(self, func, args):  # noqa: ANN001, ARG002
        return _TimeoutFuture()

    def terminate(self):
        self.terminated = True

    def close(self):
        self.closed = True

    def join(self):
        self.join_count += 1


class _TimeoutContext:
    def __init__(self, pool: _TimeoutPool):
        self.pool = pool

    def Pool(self, processes: int):  # noqa: N802, ARG002
        return self.pool


def test_cascadio_timeout_terminates_pool_without_close(monkeypatch):
    """multiprocessing timeouts must terminate the worker instead of joining it."""
    from src.core import geometry

    pool = _TimeoutPool()

    monkeypatch.setattr(
        multiprocessing,
        "get_context",
        lambda _method: _TimeoutContext(pool),
    )

    with pytest.raises(TimeoutError, match="cascadio timed out"):
        geometry._load_cad_with_cascadio_isolated(b"STEP DATA", timeout_seconds=1)

    assert pool.terminated is True
    assert pool.closed is False
    assert pool.join_count == 1
