"""Tests for the browser-first geometry runtime worker."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKER_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "client_runtime"
    / "client-geometry-runtime.worker.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_detects_local_overhang_hints() -> None:
    script = textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const vm = require('node:vm');

        let posted = null;
        const context = {{
          console,
          TextDecoder,
          TextEncoder,
          Uint8Array,
          DataView,
          Map,
          Math,
          Number,
          Infinity,
          crypto: globalThis.crypto,
          self: {{
            crypto: globalThis.crypto,
            postMessage: message => {{ posted = message; }}
          }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        const event = {{
          data: {{
            id: 'overhang-smoke',
            processCode: 'FDM',
            input: {{
              meshBuffers: {{
                positions: [
                  0, 0, 0,
                  0, 0, 10,
                  10, 0, 10,
                  0, 10, 10
                ],
                indices: [
                  0, 1, 2,
                  1, 3, 2
                ]
              }}
            }}
          }}
        }};

        Promise.resolve(context.self.onmessage(event)).then(() => {{
          if (!posted) {{
            console.error('worker did not post a result');
            process.exit(1);
          }}
          console.log(JSON.stringify(posted));
        }});
        """
    )

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    posted = json.loads(completed.stdout)

    assert posted["ok"] is True
    result = posted["result"]
    assert result["authority"] == "local_primary"
    assert result["executionMode"] == "primary_interactive"
    assert "triangles" not in result["metrics"]
    overhang = next(
        issue for issue in result["issues"] if issue["category"] == "overhang"
    )
    assert overhang["faceIndices"] == [1]
    overhang_hint = next(
        hint for hint in result["localOverlayHints"] if hint["category"] == "overhang"
    )
    assert overhang_hint["faceIndices"] == [1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_accepts_obj_file_bytes() -> None:
    obj_source = textwrap.dedent(
        """
        v 0 0 0
        v 10 0 0
        v 10 20 0
        v 0 20 0
        f 1 2 3 4
        """
    ).strip()
    script = textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const vm = require('node:vm');

        let posted = null;
        const context = {{
          console,
          TextDecoder,
          TextEncoder,
          Uint8Array,
          DataView,
          Map,
          Math,
          Number,
          Infinity,
          crypto: globalThis.crypto,
          self: {{
            crypto: globalThis.crypto,
            postMessage: message => {{ posted = message; }}
          }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        const objSource = {json.dumps(obj_source)};
        const event = {{
          data: {{
            id: 'obj-bytes',
            processCode: 'CNC_MILL',
            input: {{
              fileName: 'plate.obj',
              fileBytes: new TextEncoder().encode(objSource)
            }}
          }}
        }};

        Promise.resolve(context.self.onmessage(event)).then(() => {{
          if (!posted) {{
            console.error('worker did not post a result');
            process.exit(1);
          }}
          console.log(JSON.stringify(posted));
        }});
        """
    )

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    posted = json.loads(completed.stdout)

    assert posted["ok"] is True
    result = posted["result"]
    assert result["authority"] == "local_primary"
    assert result["processCode"] == "CNC_MILL"
    assert result["metrics"]["faceCount"] == 2
    assert result["metrics"]["boundingBox"] == {"x": 10, "y": 20, "z": 0}
