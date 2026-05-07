"""Run every generator script in this directory to (re)build all fixtures.

Usage::

    poetry run python tests/fixtures/dfm/generate/generate_all.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

GENERATE_DIR = Path(__file__).resolve().parent


def main() -> None:
    here = GENERATE_DIR
    # Skip helper scripts (leading-underscore convention) and this dispatcher.
    scripts = sorted(
        p for p in here.glob("*.py")
        if p.name not in {"__init__.py", "generate_all.py"}
        and not p.name.startswith("_")
    )
    for script in scripts:
        spec = importlib.util.spec_from_file_location(script.stem, script)
        if spec is None or spec.loader is None:
            print(f"SKIP {script.name} (no spec)")
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "main"):
            print(f"--- {script.name} ---")
            module.main()


if __name__ == "__main__":
    main()
