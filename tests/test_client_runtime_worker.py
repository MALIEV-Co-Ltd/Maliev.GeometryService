"""Tests for the browser-first geometry runtime worker."""

from __future__ import annotations

import base64
import json
import re
import shutil
import struct
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

WORKER_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "client_runtime"
    / "client-geometry-runtime.worker.js"
)
def _worker_source_version() -> str:
    """The version the worker source declares — tests must never pin a literal.

    Services always pull the LATEST runtime from GeometryService; asserting a
    hardcoded version here would re-introduce the pinning this architecture
    forbids.
    """
    source = WORKER_PATH.read_text(encoding="utf-8")
    match = re.search(
        r'MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION = "([^"]+)"', source
    )
    assert match, "worker source must declare MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION"
    return match.group(1)



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
    assert result["runtimeVersion"] == _worker_source_version()
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
          positions[t * 9 + 0] = x;
          positions[t * 9 + 1] = y;
          positions[t * 9 + 2] = 0;
          positions[t * 9 + 3] = x + 1;
          positions[t * 9 + 4] = y;
          positions[t * 9 + 5] = 0;
          positions[t * 9 + 6] = x;
          positions[t * 9 + 7] = y + 1;
          positions[t * 9 + 8] = 0;
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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_welds_vertices_and_counts_bodies() -> None:
    """Viewer meshes duplicate vertices per face. The worker must weld them by
    position before topology analysis: an unwelded but watertight tetrahedron is
    manifold (zero bogus non-manifold edges), and two disjoint tetrahedra report
    bodyCount=2 with a multi_body issue via the metrics-only operation."""
    script = textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const vm = require('node:vm');

        let posted = null;
        const context = {{
          console, TextDecoder, TextEncoder, Uint8Array, DataView, Map, Math,
          Number, Infinity, Set, Array,
          crypto: globalThis.crypto,
          self: {{ crypto: globalThis.crypto, postMessage: m => {{ posted = m; }} }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        // Watertight tetrahedron with EVERY face using its own duplicated
        // vertices (12 positions for 4 shared corners), offset by dx.
        function tetraPositions(dx) {{
          const v0 = [dx + 0, 0, 0];
          const v1 = [dx + 10, 0, 0];
          const v2 = [dx + 0, 10, 0];
          const v3 = [dx + 0, 0, 10];
          const faces = [
            [v0, v2, v1],
            [v0, v1, v3],
            [v0, v3, v2],
            [v1, v2, v3],
          ];
          return faces.flat(2);
        }}
        const positions = [...tetraPositions(0), ...tetraPositions(100)];
        const indices = Array.from({{ length: positions.length / 3 }}, (_, i) => i);

        const event = {{
          data: {{
            id: 'weld-smoke',
            operation: 'compute_metrics',
            input: {{ meshBuffers: {{ positions, indices }} }}
          }}
        }};

        Promise.resolve(context.self.onmessage(event)).then(() => {{
          console.log(JSON.stringify(posted));
        }});
        """
    )

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    posted = json.loads(completed.stdout)

    assert posted["ok"] is True, posted.get("error")
    result = posted["result"]
    assert result["operation"] == "compute_metrics"
    assert result["processCode"] is None
    metrics = result["metrics"]
    assert metrics["isManifold"] is True
    assert metrics["nonManifoldEdgeCount"] == 0
    assert metrics["openEdgeCount"] == 0
    assert metrics["bodyCount"] == 2
    assert "weldedIndices" not in metrics
    categories = [issue["category"] for issue in result["issues"]]
    assert "multi_body" in categories
    assert "mesh_integrity" not in categories


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_keeps_touching_source_bodies_topologically_separate() -> None:
    """Separate CAD/viewer bodies may touch or share coincident faces. The
    worker must not weld across source mesh-buffer boundaries; otherwise a valid
    multi-body CAD file is collapsed into one non-manifold topology graph."""
    script = textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const vm = require('node:vm');

        let posted = null;
        const context = {{
          console, TextDecoder, TextEncoder, Uint8Array, DataView, Map, Math,
          Number, Infinity, Set, Array,
          crypto: globalThis.crypto,
          self: {{ crypto: globalThis.crypto, postMessage: m => {{ posted = m; }} }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        function cubeMesh(x0) {{
          const x1 = x0 + 10;
          return {{
            positions: [
              x0, 0, 0,  x1, 0, 0,  x1, 10, 0,  x0, 10, 0,
              x0, 0, 10, x1, 0, 10, x1, 10, 10, x0, 10, 10
            ],
            indices: [
              0, 2, 1, 0, 3, 2,
              4, 5, 6, 4, 6, 7,
              0, 1, 5, 0, 5, 4,
              1, 2, 6, 1, 6, 5,
              2, 3, 7, 2, 7, 6,
              3, 0, 4, 3, 4, 7
            ]
          }};
        }}

        const event = {{
          data: {{
            id: 'touching-source-bodies',
            operation: 'compute_metrics',
            input: {{ meshBuffers: [cubeMesh(0), cubeMesh(10)] }}
          }}
        }};

        Promise.resolve(context.self.onmessage(event)).then(() => {{
          console.log(JSON.stringify(posted));
        }});
        """
    )

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    posted = json.loads(completed.stdout)

    assert posted["ok"] is True, posted.get("error")
    result = posted["result"]
    metrics = result["metrics"]
    assert metrics["isManifold"] is True
    assert metrics["nonManifoldEdgeCount"] == 0
    assert metrics["openEdgeCount"] == 0
    assert metrics["bodyCount"] == 2
    categories = [issue["category"] for issue in result["issues"]]
    assert "multi_body" in categories
    assert "mesh_integrity" not in categories


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_overhang_uses_45_degree_rule_and_regions() -> None:
    """Overhang detection: faces steeper than 45° downward are flagged (with a
    region count in the description); shallower downward faces and upward faces
    are not."""
    script = textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const vm = require('node:vm');

        let posted = null;
        const context = {{
          console, TextDecoder, TextEncoder, Uint8Array, DataView, Map, Math,
          Number, Infinity, Set, Array,
          crypto: globalThis.crypto,
          self: {{ crypto: globalThis.crypto, postMessage: m => {{ posted = m; }} }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        const positions = [
          // face 0: build plate at z=0 (excluded from overhang checks)
          0, 0, 0,   10, 0, 0,   0, 10, 0,
          // face 1: steep downward face, normal ≈ (0, 0.5, -0.866) — 60° from
          // horizontal normal direction → steeper than the 45° limit → flagged
          0, 0, 5,   0, 1, 5.577,   10, 0, 5,
          // face 2: shallow downward face, normal ≈ (0, 0.8, -0.6) — inside the
          // 45° self-supporting limit → NOT flagged
          20, 0, 5,  20, 1, 6.333,  30, 0, 5,
          // face 3: upward face, normal (0, 0, 1) → NOT flagged
          40, 0, 5,  50, 0, 5,  40, 1, 5,
        ];
        const indices = Array.from({{ length: positions.length / 3 }}, (_, i) => i);

        const event = {{
          data: {{
            id: 'overhang-45',
            processCode: 'FDM',
            input: {{ meshBuffers: {{ positions, indices }} }}
          }}
        }};

        Promise.resolve(context.self.onmessage(event)).then(() => {{
          console.log(JSON.stringify(posted));
        }});
        """
    )

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    posted = json.loads(completed.stdout)

    assert posted["ok"] is True, posted.get("error")
    overhang = next(
        issue for issue in posted["result"]["issues"] if issue["category"] == "overhang"
    )
    assert overhang["faceIndices"] == [1]
    assert overhang["value"] == 1  # one connected overhang region
    assert "1 overhang region" in overhang["description"]
    assert "45" in overhang["description"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_client_runtime_worker_repairs_inconsistent_seam_winding() -> None:
    """A steep downward-facing quad split into two triangles, one of which is
    deliberately wound backwards (as can happen at a tessellation seam, e.g.
    where an open cavity's rim meets the outer wall). Without a winding
    repair pass, the backwards triangle's raw cross-product normal points
    the wrong way and is silently skipped, even though it is part of the
    same overhang surface as its correctly-wound neighbour. The repair must
    detect the disagreement across their shared edge and flip it back."""
    script = textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const vm = require('node:vm');

        let posted = null;
        const context = {{
          console, TextDecoder, TextEncoder, Uint8Array, DataView, Map, Math,
          Number, Infinity, Set, Array,
          crypto: globalThis.crypto,
          self: {{ crypto: globalThis.crypto, postMessage: m => {{ posted = m; }} }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        const positions = [
          // face 0: build plate at z=0 (excluded from overhang checks)
          0, 0, 0,   10, 0, 0,   0, 10, 0,
          // quad corners of a single steep (60° from horizontal) surface,
          // split along the corner00-corner11 diagonal:
          0, 0, 5,      // corner00 (index 3)
          0, 1, 5.577,  // corner01 (index 4)
          10, 1, 5.577, // corner11 (index 5)
          10, 0, 5,     // corner10 (index 6)
        ];
        const indices = [
          0, 1, 2,
          3, 4, 5, // face 1: correctly wound — normal ~= (0, 0.5, -0.866)
          3, 6, 5, // face 2: SAME physical surface, vertices reversed —
                   // raw normal ~= (0, -0.5, 0.866) without the repair pass
        ];

        const event = {{
          data: {{
            id: 'seam-winding',
            processCode: 'FDM',
            input: {{ meshBuffers: {{ positions, indices }} }}
          }}
        }};

        Promise.resolve(context.self.onmessage(event)).then(() => {{
          console.log(JSON.stringify(posted));
        }});
        """
    )

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    posted = json.loads(completed.stdout)

    assert posted["ok"] is True, posted.get("error")
    overhang = next(
        issue for issue in posted["result"]["issues"] if issue["category"] == "overhang"
    )
    # Both triangles of the single physical surface must be flagged, not just
    # the one that happened to be wound correctly in the source mesh.
    assert overhang["faceIndices"] == [1, 2]
    assert overhang["value"] == 1  # one connected overhang region


# ---------------------------------------------------------------------------
# v2 DFM engine: full client-side process screening
# ---------------------------------------------------------------------------

_MESH_BUILDER_JS = """
function pushTri(pos, idx, a, b, c) {
  const base = pos.length / 3;
  pos.push(...a, ...b, ...c);
  idx.push(base, base + 1, base + 2);
}
function quad(pos, idx, a, b, c, d) {
  pushTri(pos, idx, a, b, c);
  pushTri(pos, idx, a, c, d);
}
function box(pos, idx, cx, cy, cz, sx, sy, sz) {
  // Outward-wound faces (CAD/STL convention): bottom -Z, top +Z, etc.
  const x0 = cx - sx / 2, x1 = cx + sx / 2;
  const y0 = cy - sy / 2, y1 = cy + sy / 2;
  const z0 = cz - sz / 2, z1 = cz + sz / 2;
  quad(pos, idx, [x0,y0,z0],[x0,y1,z0],[x1,y1,z0],[x1,y0,z0]);
  quad(pos, idx, [x0,y0,z1],[x1,y0,z1],[x1,y1,z1],[x0,y1,z1]);
  quad(pos, idx, [x0,y0,z0],[x0,y0,z1],[x0,y1,z1],[x0,y1,z0]);
  quad(pos, idx, [x1,y0,z0],[x1,y1,z0],[x1,y1,z1],[x1,y0,z1]);
  quad(pos, idx, [x0,y0,z0],[x1,y0,z0],[x1,y0,z1],[x0,y0,z1]);
  quad(pos, idx, [x0,y1,z0],[x0,y1,z1],[x1,y1,z1],[x1,y1,z0]);
}
function cylinder(pos, idx, cx, cy, z0, z1, r, seg) {
  seg = seg || 48;
  for (let s = 0; s < seg; s += 1) {
    const a0 = (s / seg) * 2 * Math.PI;
    const a1 = ((s + 1) / seg) * 2 * Math.PI;
    const p0 = [cx + r * Math.cos(a0), cy + r * Math.sin(a0)];
    const p1 = [cx + r * Math.cos(a1), cy + r * Math.sin(a1)];
    quad(pos, idx,
      [p0[0],p0[1],z0], [p1[0],p1[1],z0], [p1[0],p1[1],z1], [p0[0],p0[1],z1]);
    pushTri(pos, idx, [cx,cy,z1], [p0[0],p0[1],z1], [p1[0],p1[1],z1]);
    pushTri(pos, idx, [cx,cy,z0], [p1[0],p1[1],z0], [p0[0],p0[1],z0]);
  }
}
"""


def _run_worker_dfm(build_js: str, process_code: str) -> dict:
    """Run the worker's analyze() on a JS-built mesh; return the posted result."""
    mesh_script = (
        _MESH_BUILDER_JS
        + chr(10)
        + "const pos = []; const idx = [];"
        + chr(10)
        + build_js
        + chr(10)
        + "({ positions: pos, indices: idx })"
    )
    script = textwrap.dedent(
        f"""
        const fs = require('node:fs');
        const vm = require('node:vm');

        let posted = null;
        const context = {{
          console, TextDecoder, TextEncoder, Uint8Array, Uint32Array,
          Float64Array, DataView, Map, Set, Math, Number, Infinity, JSON,
          Array, Promise, Float32Array,
          crypto: globalThis.crypto,
          self: {{
            crypto: globalThis.crypto,
            postMessage: message => {{ posted = message; }}
          }}
        }};
        vm.createContext(context);
        const workerPath = {json.dumps(str(WORKER_PATH))};
        vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context);

        const mesh = vm.runInContext(
          {json.dumps(mesh_script)},
          vm.createContext({{ Math }})
        );

        const event = {{
          data: {{
            id: 'dfm-v2',
            processCode: {json.dumps(process_code)},
            input: {{ meshBuffers: mesh }}
          }}
        }};
        Promise.resolve(context.self.onmessage(event)).then(() => {{
          if (!posted) {{ console.error('no result'); process.exit(1); }}
          console.log(JSON.stringify(posted));
        }});
        """
    )
    # Node 24's `-e` string evaluator mis-parses large embedded sources on
    # Windows; run from a real .cjs file instead.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        completed = subprocess.run(
            ["node", script_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    posted = json.loads(completed.stdout)
    assert posted["ok"] is True, posted
    return posted["result"]


def _categories(result: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in result["issues"]:
        counts[issue["category"]] = counts.get(issue["category"], 0) + 1
    return counts


def _issue(result: dict, category: str) -> dict:
    return next(issue for issue in result["issues"] if issue["category"] == category)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dfm_v2_every_issue_carries_overlay_faces() -> None:
    result = _run_worker_dfm(
        "cylinder(pos, idx, 0, 0, 0, 10, 3); cylinder(pos, idx, 0, 0, 10, 14, 12);",
        "FDM",
    )
    for issue in result["issues"]:
        assert isinstance(issue["faceIndices"], list), issue["category"]
    hints = {hint["category"] for hint in result["localOverlayHints"]}
    assert "overhang" in hints
    overhang = _issue(result, "overhang")
    assert len(overhang["faceIndices"]) > 0  # exact-region GLB overlay support


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dfm_v2_warpage_on_large_thin_plate_only() -> None:
    plate = _run_worker_dfm("box(pos, idx, 0, 0, 1, 120, 90, 2);", "FDM")
    assert "warpage" in _categories(plate)
    assert len(_issue(plate, "warpage")["faceIndices"]) > 0

    cube = _run_worker_dfm("box(pos, idx, 0, 0, 12.5, 25, 25, 25);", "FDM")
    for absent in ("warpage", "overhang", "thin_wall", "unsupported_wall"):
        assert absent not in _categories(cube), absent


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dfm_v2_thin_fin_fires_thin_and_unsupported_wall() -> None:
    build = "box(pos, idx, 0, 0, 2.5, 40, 40, 5); box(pos, idx, 0, 0, 11, 30, 0.6, 12);"
    result = _run_worker_dfm(build, "FDM")
    cats = _categories(result)
    assert "thin_wall" in cats
    assert "unsupported_wall" in cats


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dfm_v2_thin_pin_below_process_minimum() -> None:
    build = "cylinder(pos, idx, 0, 0, 0, 4, 10); cylinder(pos, idx, 0, 0, 4, 9, 0.3);"
    result = _run_worker_dfm(build, "SLS")
    assert "pin" in _categories(result)
    pin = _issue(result, "pin")
    assert pin["threshold"] == 0.8  # SLS pin minimum from the rules table


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dfm_v2_turning_symmetry_and_slenderness() -> None:
    turnable = _run_worker_dfm("cylinder(pos, idx, 0, 0, 0, 60, 8, 64);", "CNC_TURN")
    assert "not_turnable" not in _categories(turnable)

    box_result = _run_worker_dfm("box(pos, idx, 0, 0, 10, 40, 20, 20);", "CNC_TURN")
    assert "not_turnable" in _categories(box_result)

    shaft = _run_worker_dfm("cylinder(pos, idx, 0, 0, 0, 100, 5, 64);", "CNC_TURN")
    assert "ld_ratio" in _categories(shaft)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dfm_v2_sheet_metal_uniformity() -> None:
    sheet = _run_worker_dfm("box(pos, idx, 0, 0, 1, 80, 60, 2);", "SHEET_METAL")
    assert "sheet_uniformity" not in _categories(sheet)

    cube = _run_worker_dfm("box(pos, idx, 0, 0, 12.5, 25, 25, 25);", "SHEET_METAL")
    assert "sheet_uniformity" in _categories(cube)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dfm_v2_silicone_thin_wall() -> None:
    build = "box(pos, idx, 0, 0, 2.5, 40, 40, 5); box(pos, idx, 0, 0, 10, 30, 0.4, 10);"
    result = _run_worker_dfm(build, "SILICONE_CASTING")
    assert "thin_wall" in _categories(result)
    assert _issue(result, "thin_wall")["threshold"] == 0.5


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dfm_v2_powder_clearance_between_bodies() -> None:
    build = "box(pos, idx, 0, 0, 5, 20, 20, 10); box(pos, idx, 0, 0, 15.2, 20, 20, 10);"
    result = _run_worker_dfm(build, "SLS")
    cats = _categories(result)
    assert "multi_body" in cats
    assert "clearance" in cats  # 0.2 mm gap < 0.3 mm SLS moving-part clearance


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_dfm_v2_powder_bed_skips_overhangs() -> None:
    build = "cylinder(pos, idx, 0, 0, 0, 10, 3); cylinder(pos, idx, 0, 0, 10, 14, 12);"
    result = _run_worker_dfm(build, "SLS")
    assert "overhang" not in _categories(result)
