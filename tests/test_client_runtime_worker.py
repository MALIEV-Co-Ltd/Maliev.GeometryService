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
KERNEL_WASM_BASE64 = (
    "AGFzbQEAAAABEANgAAF/YAF/AX9gAn9/AX8DBAMAAQIHQwMPcnVudGltZV92ZXJzaW9u"
    "AAAbdHJpYW5nbGVfY291bnRfZnJvbV9pbmRpY2VzAAEPaXNfd2l0aGluX2xpbWl0AAIK"
    "FgMEAEEBCwcAIABBA24LBwAgACABTQs="
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
def test_client_runtime_worker_loads_manifest_wasm_kernel() -> None:
    script = textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const vm = require('node:vm');

        let posted = null;
        let fetchedUrl = null;
        const wasmBase64 = {json.dumps(KERNEL_WASM_BASE64)};
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
          WebAssembly,
          crypto: globalThis.crypto,
          JSON,
          fetch: async url => {{
            fetchedUrl = String(url);
            const bytes = Buffer.from(wasmBase64, 'base64');
            const end = bytes.byteOffset + bytes.byteLength;
            return {{
              ok: true,
              arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, end)
            }};
          }},
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
            id: 'wasm-kernel',
            processCode: 'CNC_MILL',
            wasmUrl: '/geometry/client-runtime/assets/client-geometry-kernel.test.wasm',
            input: {{
              meshBuffers: [{{
                positions: [0, 0, 0, 10, 0, 0, 10, 20, 0, 0, 20, 0],
                indices: [0, 1, 2, 0, 2, 3]
              }}]
            }}
          }}
        }};

        Promise.resolve(context.self.onmessage(event)).then(() => {{
          if (!posted) {{
            console.error('worker did not post a result');
            process.exit(1);
          }}
          console.log(JSON.stringify({{ posted, fetchedUrl }}));
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
    probe = json.loads(completed.stdout)
    posted = probe["posted"]

    assert probe["fetchedUrl"] == (
        "/geometry/client-runtime/assets/client-geometry-kernel.test.wasm"
    )
    assert posted["ok"] is True
    result = posted["result"]
    assert result["runtimeKernel"] == {
        "wasmLoaded": True,
        "runtimeVersion": 1,
    }
    assert result["metrics"]["faceCount"] == 2


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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_accepts_compressed_3mf_file_bytes() -> None:
    fixture_path = Path(__file__).parent / "assets" / "50x50x50mm-solid-cube.3mf"
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
          Blob,
          Response,
          DecompressionStream,
          crypto: globalThis.crypto,
          JSON,
          self: {{
            crypto: globalThis.crypto,
            postMessage: message => {{ posted = message; }}
          }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        const fixturePath = {json.dumps(str(fixture_path))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        const event = {{
          data: {{
            id: '3mf-bytes',
            processCode: 'CNC_MILL',
            input: {{
              fileName: 'cube.3mf',
              fileBytes: new Uint8Array(fs.readFileSync(fixturePath))
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
    assert result["metrics"]["faceCount"] == 12
    assert result["metrics"]["boundingBox"] == {"x": 50, "y": 50, "z": 50}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_extracts_mesh_buffers_from_compressed_3mf() -> None:
    fixture_path = Path(__file__).parent / "assets" / "50x50x50mm-solid-cube.3mf"
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
          Blob,
          Response,
          DecompressionStream,
          crypto: globalThis.crypto,
          JSON,
          self: {{
            crypto: globalThis.crypto,
            postMessage: message => {{ posted = message; }}
          }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        const fixturePath = {json.dumps(str(fixture_path))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        const event = {{
          data: {{
            id: '3mf-mesh-extract',
            operation: 'extract_mesh',
            input: {{
              fileName: 'cube.3mf',
              fileBytes: new Uint8Array(fs.readFileSync(fixturePath))
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
    assert result["runtimeVersion"] == "1.1.1"
    assert result["operation"] == "extract_mesh"
    assert result["sourceFormat"] == "3mf"
    assert result["meshBuffers"]["positions"][0:3] == [-25, -25, 0]
    assert len(result["meshBuffers"]["positions"]) == 24
    assert len(result["meshBuffers"]["indices"]) == 36
    assert result["metrics"]["faceCount"] == 12
    assert result["metrics"]["boundingBox"] == {"x": 50, "y": 50, "z": 50}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_analyzes_large_viewer_meshes() -> None:
    """Large viewer meshes used to crash the worker with a call-stack overflow
    (positions were appended via push(...spread)). The DFM run then fell back
    to the server with 'worker_failed'. 60k triangles exceeds the V8 argument
    limit, so this test fails if the spread pattern ever returns."""
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

        // 60,000 disconnected upward-facing triangles laid out on a grid.
        const triangleCount = 60000;
        const positions = new Array(triangleCount * 9);
        const indices = new Array(triangleCount * 3);
        for (let t = 0; t < triangleCount; t += 1) {{
          const x = (t % 300) * 2;
          const y = Math.floor(t / 300) * 2;
          positions[t * 9 + 0] = x;     positions[t * 9 + 1] = y;     positions[t * 9 + 2] = 0;
          positions[t * 9 + 3] = x + 1; positions[t * 9 + 4] = y;     positions[t * 9 + 5] = 0;
          positions[t * 9 + 6] = x;     positions[t * 9 + 7] = y + 1; positions[t * 9 + 8] = 0;
          indices[t * 3 + 0] = t * 3;
          indices[t * 3 + 1] = t * 3 + 1;
          indices[t * 3 + 2] = t * 3 + 2;
        }}

        const event = {{
          data: {{
            id: 'large-mesh-smoke',
            processCode: 'CNC_MILL',
            input: {{ meshBuffers: {{ positions, indices }} }}
          }}
        }};

        Promise.resolve(context.self.onmessage(event)).then(() => {{
          if (!posted) {{
            console.error('worker did not post a result');
            process.exit(1);
          }}
          console.log(JSON.stringify({{
            ok: posted.ok,
            error: posted.error ?? null,
            faceCount: posted.result?.metrics?.faceCount ?? null,
            authority: posted.result?.authority ?? null
          }}));
        }});
        """
    )

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    posted = json.loads(completed.stdout)

    assert posted["ok"] is True, f"worker failed on large mesh: {posted['error']}"
    assert posted["faceCount"] == 60000
    assert posted["authority"] == "local_primary"
