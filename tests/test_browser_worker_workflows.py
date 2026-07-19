"""Regression tests for cost-bounded browser-worker GitHub workflows."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = REPO_ROOT / ".github" / "workflows"
PR_VALIDATION = WORKFLOW_ROOT / "pr-validation.yml"
REPEAT_VALIDATION = WORKFLOW_ROOT / "real-browser-worker-repeat.yml"


def _job_block(source: str, job_name: str) -> str:
    marker = f"  {job_name}:\n"
    assert marker in source, f"missing workflow job: {job_name}"
    remainder = source.split(marker, maxsplit=1)[1]
    next_job = re.search(r"^  [a-zA-Z0-9_-]+:\n", remainder, re.MULTILINE)
    return remainder[: next_job.start()] if next_job else remainder


def test_normal_pr_validation_runs_one_real_browser_smoke() -> None:
    source = PR_VALIDATION.read_text(encoding="utf-8")
    smoke_job = _job_block(source, "real-browser-worker-smoke")

    assert "matrix:" not in smoke_job
    assert "repetition:" not in smoke_job
    assert "node --test tests/client-runtime-browser-smoke.test.mjs" in smoke_job
    assert "timeout-minutes: 5" in smoke_job


def test_repeat_validation_is_explicit_and_browser_path_scoped() -> None:
    source = REPEAT_VALIDATION.read_text(encoding="utf-8")
    smoke_job = _job_block(source, "real-browser-worker-repeat")

    assert "workflow_dispatch:" in source
    assert "pull_request:" in source
    assert "paths:" in source
    for path in (
        "src/client_runtime/**",
        "tests/client-runtime-browser-smoke.test.mjs",
        "tests/test_client_runtime_browser_smoke.py",
        ".github/workflows/real-browser-worker-repeat.yml",
    ):
        assert f'- "{path}"' in source

    assert "permissions:\n  contents: read" in source
    assert "cancel-in-progress: true" in source
    assert "timeout-minutes: 5" in smoke_job
    assert "matrix:" not in smoke_job
    assert "for repetition in $(seq 1 10)" in smoke_job
    assert "Real browser repetition ${repetition}/10" in smoke_job
    assert re.search(r"actions/checkout@[0-9a-f]{40}", smoke_job)
    assert "persist-credentials: false" in smoke_job
    assert "node --test" in smoke_job
    assert "--test-name-pattern" in smoke_job
    assert "tests/client-runtime-browser-smoke.test.mjs" in smoke_job
