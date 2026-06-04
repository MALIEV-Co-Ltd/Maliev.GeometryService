"""Tests for the browser-first geometry runtime worker."""

from __future__ import annotations

import base64
import json
import shutil
import struct
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


def _pad_glb_chunk(data: bytes, pad_byte: bytes) -> bytes:
    remainder = len(data) % 4
    if remainder == 0:
        return data
    return data + (pad_byte * (4 - remainder))


def _tiny_plate_glb_bytes() -> bytes:
    positions = struct.pack(
        "<12f",
        0.0,
        0.0,
        0.0,
        10.0,
        0.0,
        0.0,
        10.0,
        20.0,
        0.0,
        0.0,
        20.0,
        0.0,
    )
    indices = struct.pack("<6H", 0, 1, 2, 0, 2, 3)
    binary_chunk = _pad_glb_chunk(positions, b"\x00") + _pad_glb_chunk(indices, b"\x00")
    json_chunk = _pad_glb_chunk(
        json.dumps(
            {
                "asset": {"version": "2.0"},
                "buffers": [{"byteLength": len(binary_chunk)}],
                "bufferViews": [
                    {
                        "buffer": 0,
                        "byteOffset": 0,
                        "byteLength": len(positions),
                        "target": 34962,
                    },
                    {
                        "buffer": 0,
                        "byteOffset": len(_pad_glb_chunk(positions, b"\x00")),
                        "byteLength": len(indices),
                        "target": 34963,
                    },
                ],
                "accessors": [
                    {
                        "bufferView": 0,
                        "componentType": 5126,
                        "count": 4,
                        "type": "VEC3",
                    },
                    {
                        "bufferView": 1,
                        "componentType": 5123,
                        "count": 6,
                        "type": "SCALAR",
                    },
                ],
                "meshes": [
                    {
                        "primitives": [
                            {
                                "attributes": {"POSITION": 0},
                                "indices": 1,
                            }
                        ]
                    }
                ],
                "nodes": [{"mesh": 0}],
                "scenes": [{"nodes": [0]}],
                "scene": 0,
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        b" ",
    )
    length = 12 + 8 + len(json_chunk) + 8 + len(binary_chunk)
    return b"".join(
        [
            struct.pack("<III", 0x46546C67, 2, length),
            struct.pack("<II", len(json_chunk), 0x4E4F534A),
            json_chunk,
            struct.pack("<II", len(binary_chunk), 0x004E4942),
            binary_chunk,
        ]
    )


def _tiny_plate_gltf_source() -> str:
    positions = struct.pack(
        "<12f",
        0.0,
        0.0,
        0.0,
        10.0,
        0.0,
        0.0,
        10.0,
        20.0,
        0.0,
        0.0,
        20.0,
        0.0,
    )
    indices = struct.pack("<6H", 0, 1, 2, 0, 2, 3)
    binary_chunk = _pad_glb_chunk(positions, b"\x00") + _pad_glb_chunk(indices, b"\x00")
    return json.dumps(
        {
            "asset": {"version": "2.0"},
            "buffers": [
                {
                    "byteLength": len(binary_chunk),
                    "uri": "data:application/octet-stream;base64,"
                    + base64.b64encode(binary_chunk).decode("ascii"),
                }
            ],
            "bufferViews": [
                {
                    "buffer": 0,
                    "byteOffset": 0,
                    "byteLength": len(positions),
                    "target": 34962,
                },
                {
                    "buffer": 0,
                    "byteOffset": len(_pad_glb_chunk(positions, b"\x00")),
                    "byteLength": len(indices),
                    "target": 34963,
                },
            ],
            "accessors": [
                {
                    "bufferView": 0,
                    "componentType": 5126,
                    "count": 4,
                    "type": "VEC3",
                },
                {
                    "bufferView": 1,
                    "componentType": 5123,
                    "count": 6,
                    "type": "SCALAR",
                },
            ],
            "meshes": [
                {
                    "primitives": [
                        {
                            "attributes": {"POSITION": 0},
                            "indices": 1,
                        }
                    ]
                }
            ],
            "nodes": [{"mesh": 0}],
            "scenes": [{"nodes": [0]}],
            "scene": 0,
        },
        separators=(",", ":"),
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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_accepts_gltf_file_bytes() -> None:
    gltf_source = _tiny_plate_gltf_source()
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
          JSON,
          atob: value => Buffer.from(value, 'base64').toString('binary'),
          self: {{
            crypto: globalThis.crypto,
            postMessage: message => {{ posted = message; }}
          }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        const gltfSource = {json.dumps(gltf_source)};
        const event = {{
          data: {{
            id: 'gltf-bytes',
            processCode: 'CNC_MILL',
            input: {{
              fileName: 'plate.gltf',
              fileBytes: new TextEncoder().encode(gltfSource)
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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_accepts_glb_file_bytes() -> None:
    glb_bytes = _tiny_plate_glb_bytes()
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
          JSON,
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
            id: 'glb-bytes',
            processCode: 'CNC_MILL',
            input: {{
              fileName: 'plate.glb',
              fileBytes: Uint8Array.from({list(glb_bytes)})
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
