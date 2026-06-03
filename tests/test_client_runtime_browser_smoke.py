"""Pytest entrypoint for the browser-first runtime e2e smoke."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

BROWSER_SMOKE_PATH = Path(__file__).with_name("client-runtime-browser-smoke.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_browser_smoke_executes_real_web_worker() -> None:
    completed = subprocess.run(
        ["node", "--test", str(BROWSER_SMOKE_PATH)],
        capture_output=True,
        text=True,
        timeout=70,
    )

    if "Chrome or Edge is required" in completed.stdout:
        pytest.skip("Chrome or Edge is required for browser runtime e2e smoke.")

    assert completed.returncode == 0, completed.stdout + completed.stderr
