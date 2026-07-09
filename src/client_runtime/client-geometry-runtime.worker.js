const MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION = "1.3.0";
const MALIEV_BROWSER_GEOMETRY_ALGORITHM_VERSION = "browser-first-dfm-v1";
const MALIEV_BROWSER_GEOMETRY_EXECUTION_MODE = "primary_interactive";

let runtimeKernelPromise = null;
let runtimeKernelUrl = null;

self.onmessage = async event => {
  const message = event.data || {};
  try {
    const runtimeKernel = await loadRuntimeKernel(
      message.wasmUrl || message.runtimeKernel?.wasmUrl || message.kernelAssetUrl
    );
    const operation = String(message.operation || message.input?.operation || "analyze").toLowerCase();
    const result = operation === "extract_mesh"
      ? await extractMesh(message.input || {}, runtimeKernel)
      : operation === "compute_metrics"
        ? await computeMetricsOnly(message.input || {}, runtimeKernel)
        : await analyze(message.input || {}, message.processCode || "FDM", runtimeKernel);
    self.postMessage({ id: message.id || null, ok: true, result });
  } catch (error) {
    self.postMessage({
      id: message.id || null,
      ok: false,
      error: error instanceof Error ? error.message : String(error)
    });
  }
};

async function loadRuntimeKernel(wasmUrl) {
  const url = String(wasmUrl || "");
  if (!url || typeof WebAssembly === "undefined" || typeof fetch !== "function") {
    return null;
  }

  if (runtimeKernelPromise && runtimeKernelUrl === url) return runtimeKernelPromise;
  runtimeKernelUrl = url;
  runtimeKernelPromise = (async () => {
    const response = await fetch(url);
    if (!response?.ok || typeof response.arrayBuffer !== "function") return null;

    const bytes = await response.arrayBuffer();
    const instance = await WebAssembly.instantiate(bytes, {});
    const exports = instance?.instance?.exports || {};
    const runtimeVersion = Number(exports.runtime_version?.());
    if (runtimeVersion !== 1) return null;

    return {
      exports,
      runtimeVersion
    };
  })().catch(() => null);
  return runtimeKernelPromise;
}

async function analyze(input, processCode, runtimeKernel = null) {
  const mesh = input.meshBuffers
    ? meshFromBuffers(input.meshBuffers)
    : await meshFromFile(input.fileBytes, input.fileName || input.fileExtension || "");

  const metrics = computeMetrics(mesh, runtimeKernel);
  const inputHash = await hashMesh(mesh);
  const issues = buildIssues(metrics, processCode);
  const publicMetrics = { ...metrics };
  delete publicMetrics.triangles;
  delete publicMetrics.weldedIndices;

  return {
    runtimeVersion: MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION,
    algorithmVersion: MALIEV_BROWSER_GEOMETRY_ALGORITHM_VERSION,
    executionMode: MALIEV_BROWSER_GEOMETRY_EXECUTION_MODE,
    isAuthoritative: false,
    authority: "local_primary",
    serverRole: "fallback_and_final_validation",
    status: "analysis_complete",
    processCode,
    inputHash,
    runtimeKernel: {
      wasmLoaded: Boolean(runtimeKernel),
      runtimeVersion: runtimeKernel?.runtimeVersion ?? null
    },
    metrics: publicMetrics,
    issues,
    localOverlayHints: buildLocalOverlayHints(issues)
  };
}

// Metrics-only analysis used by the viewer before a manufacturing process is
// selected: mesh integrity (manifold/open edges), body count, bounding box,
// volume, and surface area — no process-specific DFM screening.
async function computeMetricsOnly(input, runtimeKernel = null) {
  const mesh = input.meshBuffers
    ? meshFromBuffers(input.meshBuffers)
    : await meshFromFile(input.fileBytes, input.fileName || input.fileExtension || "");

  const metrics = computeMetrics(mesh, runtimeKernel);
  const inputHash = await hashMesh(mesh);
  const issues = buildIntegrityIssues(metrics);
  const publicMetrics = { ...metrics };
  delete publicMetrics.triangles;
  delete publicMetrics.weldedIndices;

  return {
    runtimeVersion: MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION,
    algorithmVersion: MALIEV_BROWSER_GEOMETRY_ALGORITHM_VERSION,
    executionMode: MALIEV_BROWSER_GEOMETRY_EXECUTION_MODE,
    isAuthoritative: false,
    authority: "local_primary",
    serverRole: "fallback_and_final_validation",
    status: "metrics_complete",
    operation: "compute_metrics",
    processCode: null,
    inputHash,
    runtimeKernel: {
      wasmLoaded: Boolean(runtimeKernel),
      runtimeVersion: runtimeKernel?.runtimeVersion ?? null
    },
    metrics: publicMetrics,
    issues,
    localOverlayHints: buildLocalOverlayHints(issues)
  };
}

async function extractMesh(input, runtimeKernel = null) {
  const fileName = input.fileName || input.fileExtension || "";
  const mesh = input.meshBuffers
    ? meshFromBuffers(input.meshBuffers)
    : await meshFromFile(input.fileBytes, fileName);
  const metrics = computeMetrics(mesh, runtimeKernel);
  const publicMetrics = { ...metrics };
  delete publicMetrics.triangles;
  delete publicMetrics.weldedIndices;

  return {
    runtimeVersion: MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION,
    algorithmVersion: MALIEV_BROWSER_GEOMETRY_ALGORITHM_VERSION,
    executionMode: MALIEV_BROWSER_GEOMETRY_EXECUTION_MODE,
    isAuthoritative: false,
    authority: "local_primary",
    serverRole: "fallback_and_final_validation",
    operation: "extract_mesh",
    sourceFormat: input.meshBuffers ? "meshBuffers" : sourceFormatForFile(input.fileBytes, fileName),
    meshBuffers: {
      positions: mesh.positions,
      indices: mesh.indices
    },
    metrics: publicMetrics,
    runtimeKernel: {
      wasmLoaded: Boolean(runtimeKernel),
      runtimeVersion: runtimeKernel?.runtimeVersion ?? null
    }
  };
}

function meshFromBuffers(buffers) {
  const positions = [];
  const indices = [];
  const sourceGroups = [];

  const sources = Array.isArray(buffers) ? buffers : [buffers];
  for (const source of sources) {
    const baseVertex = positions.length / 3;
    const sourcePositions = Array.from(source.positions || []);
    if (sourcePositions.length % 3 !== 0) {
      throw new Error("Mesh positions must be a flat XYZ array.");
    }
    // Plain loop instead of push(...spread): spreading large viewer meshes
    // (>~65k elements) overflows the call stack and kills the analysis run.
    for (const position of sourcePositions) positions.push(position);

    const sourceIndices = Array.from(source.indices || []);
    if (sourceIndices.length > 0) {
      if (sourceIndices.length % 3 !== 0) {
        throw new Error("Mesh indices must be triangle triples.");
      }
      for (const index of sourceIndices) indices.push(baseVertex + Number(index));
    } else {
      for (let index = 0; index < sourcePositions.length / 3; index += 1) {
        indices.push(baseVertex + index);
      }
    }
    sourceGroups.push({
      startVertex: baseVertex,
      endVertex: positions.length / 3
    });
  }

  return { positions, indices, sourceGroups };
}

async function meshFromFile(fileBytes, fileName) {
  if (!fileBytes) throw new Error("No mesh bytes were provided.");
  const bytes = fileBytes instanceof Uint8Array
    ? fileBytes
    : new Uint8Array(fileBytes);
  const lowerName = String(fileName || "").toLowerCase();
  if (lowerName.endsWith(".3mf") || looksLike3mf(bytes)) {
    return await parse3mf(bytes);
  }
  if (lowerName.endsWith(".glb") || looksLikeGlb(bytes)) {
    return parseGlb(bytes);
  }
  if (lowerName.endsWith(".gltf") || looksLikeGltf(bytes)) {
    return parseGltf(bytes);
  }
  if (lowerName.endsWith(".obj") || looksLikeObj(bytes)) {
    return parseObj(bytes);
  }
  if (lowerName.endsWith(".stl") || looksLikeStl(bytes)) {
    return parseStl(bytes);
  }
  throw new Error("Browser advisory runtime v1 supports STL/OBJ/3MF/glTF/GLB bytes or viewer mesh buffers.");
}

function sourceFormatForFile(fileBytes, fileName) {
  const bytes = fileBytes instanceof Uint8Array
    ? fileBytes
    : new Uint8Array(fileBytes || []);
  const lowerName = String(fileName || "").toLowerCase();
  if (lowerName.endsWith(".3mf") || looksLike3mf(bytes)) return "3mf";
  if (lowerName.endsWith(".glb") || looksLikeGlb(bytes)) return "glb";
  if (lowerName.endsWith(".gltf") || looksLikeGltf(bytes)) return "gltf";
  if (lowerName.endsWith(".obj") || looksLikeObj(bytes)) return "obj";
  if (lowerName.endsWith(".stl") || looksLikeStl(bytes)) return "stl";
  return "unknown";
}

function looksLikeStl(bytes) {
  if (bytes.length < 6) return false;
  const prefix = new TextDecoder("ascii").decode(bytes.slice(0, Math.min(bytes.length, 80))).trimStart().toLowerCase();
  return prefix.startsWith("solid") || bytes.length >= 84;
}

function parseStl(bytes) {
  if (isLikelyBinaryStl(bytes)) return parseBinaryStl(bytes);
  return parseAsciiStl(bytes);
}

function isLikelyBinaryStl(bytes) {
  if (bytes.length < 84) return false;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const triangleCount = view.getUint32(80, true);
  return 84 + triangleCount * 50 === bytes.length;
}

function parseBinaryStl(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const triangleCount = view.getUint32(80, true);
  const positions = [];
  const indices = [];
  let offset = 84;

  for (let triangle = 0; triangle < triangleCount; triangle += 1) {
    offset += 12;
    for (let vertex = 0; vertex < 3; vertex += 1) {
      positions.push(
        view.getFloat32(offset, true),
        view.getFloat32(offset + 4, true),
        view.getFloat32(offset + 8, true)
      );
      indices.push(triangle * 3 + vertex);
      offset += 12;
    }
    offset += 2;
  }

  return { positions, indices };
}

function parseAsciiStl(bytes) {
  const text = new TextDecoder("utf-8").decode(bytes);
  const matches = text.matchAll(/vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)/g);
  const positions = [];
  const indices = [];
  let index = 0;
  for (const match of matches) {
    positions.push(Number(match[1]), Number(match[2]), Number(match[3]));
    indices.push(index);
    index += 1;
  }
  if (positions.length === 0 || indices.length % 3 !== 0) {
    throw new Error("ASCII STL did not contain complete triangle vertex data.");
  }
  return { positions, indices };
}

function looksLikeObj(bytes) {
  const prefix = new TextDecoder("utf-8").decode(bytes.slice(0, Math.min(bytes.length, 2048)));
  return /(^|\n)\s*v\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+/.test(prefix)
    && /(^|\n)\s*f\s+\S+\s+\S+\s+\S+/.test(prefix);
}

function parseObj(bytes) {
  const text = new TextDecoder("utf-8").decode(bytes);
  const sourceVertices = [];
  const positions = [];
  const indices = [];

  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.split("#", 1)[0].trim();
    if (!line) continue;
    const parts = line.split(/\s+/);
    if (parts[0] === "v" && parts.length >= 4) {
      sourceVertices.push([Number(parts[1]), Number(parts[2]), Number(parts[3])]);
      continue;
    }
    if (parts[0] !== "f" || parts.length < 4) continue;

    const faceVertexIndices = parts.slice(1)
      .map(token => parseObjVertexIndex(token, sourceVertices.length))
      .filter(index => index !== null);
    if (faceVertexIndices.length < 3) continue;

    for (let index = 1; index < faceVertexIndices.length - 1; index += 1) {
      appendObjVertex(sourceVertices, positions, indices, faceVertexIndices[0]);
      appendObjVertex(sourceVertices, positions, indices, faceVertexIndices[index]);
      appendObjVertex(sourceVertices, positions, indices, faceVertexIndices[index + 1]);
    }
  }

  if (positions.length === 0 || indices.length === 0) {
    throw new Error("OBJ did not contain complete triangle face data.");
  }
  return { positions, indices };
}

function parseObjVertexIndex(token, vertexCount) {
  const rawIndex = Number.parseInt(String(token).split("/")[0], 10);
  if (!Number.isFinite(rawIndex) || rawIndex === 0) return null;
  const index = rawIndex > 0 ? rawIndex - 1 : vertexCount + rawIndex;
  return index >= 0 && index < vertexCount ? index : null;
}

function appendObjVertex(sourceVertices, positions, indices, sourceIndex) {
  const vertex = sourceVertices[sourceIndex];
  if (!vertex) return;
  positions.push(vertex[0], vertex[1], vertex[2]);
  indices.push(indices.length);
}

function looksLike3mf(bytes) {
  if (bytes.length < 4) return false;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return view.getUint32(0, true) === 0x04034b50;
}

async function parse3mf(bytes) {
  const modelBytes = await readZipEntry(bytes, name => name.toLowerCase().endsWith(".model"));
  if (!modelBytes) {
    throw new Error("3MF archive did not contain a model entry.");
  }
  return meshFrom3mfModelXml(new TextDecoder("utf-8").decode(modelBytes));
}

async function readZipEntry(bytes, predicate) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 0;

  while (offset + 30 <= bytes.length) {
    const signature = view.getUint32(offset, true);
    if (signature === 0x02014b50 || signature === 0x06054b50) break;
    if (signature !== 0x04034b50) {
      offset += 1;
      continue;
    }

    const flags = view.getUint16(offset + 6, true);
    const compressionMethod = view.getUint16(offset + 8, true);
    const rawCompressedSize = view.getUint32(offset + 18, true);
    const rawUncompressedSize = view.getUint32(offset + 22, true);
    const fileNameLength = view.getUint16(offset + 26, true);
    const extraLength = view.getUint16(offset + 28, true);
    const fileNameStart = offset + 30;
    const fileNameEnd = fileNameStart + fileNameLength;
    const extraStart = fileNameEnd;
    const extraEnd = extraStart + extraLength;
    const zip64Sizes = readZip64ExtraSizes(
      view,
      extraStart,
      extraEnd,
      rawUncompressedSize === 0xffffffff,
      rawCompressedSize === 0xffffffff);
    const compressedSize = zip64Sizes.compressedSize ?? rawCompressedSize;
    const dataStart = extraEnd;
    const dataEnd = dataStart + compressedSize;

    if (fileNameEnd > bytes.length || extraEnd > bytes.length || dataEnd > bytes.length) break;

    const fileName = new TextDecoder((flags & 0x0800) !== 0 ? "utf-8" : "ascii")
      .decode(bytes.slice(fileNameStart, fileNameEnd));
    const entryBytes = bytes.slice(dataStart, dataEnd);
    if (predicate(fileName)) {
      return await decompressZipEntry(entryBytes, compressionMethod);
    }

    offset = dataEnd;
  }

  return null;
}

function readZip64ExtraSizes(view, start, end, needsUncompressedSize, needsCompressedSize) {
  let offset = start;
  while (offset + 4 <= end) {
    const headerId = view.getUint16(offset, true);
    const fieldSize = view.getUint16(offset + 2, true);
    const fieldStart = offset + 4;
    const fieldEnd = fieldStart + fieldSize;
    if (fieldEnd > end) break;

    if (headerId === 0x0001) {
      let cursor = fieldStart;
      let uncompressedSize = null;
      let compressedSize = null;
      if (needsUncompressedSize && cursor + 8 <= fieldEnd) {
        uncompressedSize = readZipUint64(view, cursor);
        cursor += 8;
      }
      if (needsCompressedSize && cursor + 8 <= fieldEnd) {
        compressedSize = readZipUint64(view, cursor);
      }
      return { uncompressedSize, compressedSize };
    }

    offset = fieldEnd;
  }

  return { uncompressedSize: null, compressedSize: null };
}

function readZipUint64(view, offset) {
  const low = view.getUint32(offset, true);
  const high = view.getUint32(offset + 4, true);
  const value = high * 0x100000000 + low;
  if (!Number.isSafeInteger(value)) {
    throw new Error("3MF ZIP64 entry is too large for browser-local analysis.");
  }
  return value;
}

async function decompressZipEntry(bytes, compressionMethod) {
  if (compressionMethod === 0) return bytes;
  if (compressionMethod !== 8) {
    throw new Error(`Unsupported 3MF ZIP compression method: ${compressionMethod}`);
  }
  if (typeof DecompressionStream !== "function" ||
    typeof Blob !== "function" ||
    typeof Response !== "function") {
    throw new Error("3MF ZIP deflate decompression is not available in this browser.");
  }

  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

function meshFrom3mfModelXml(xml) {
  const positions = [];
  const indices = [];
  const meshBlocks = xml.matchAll(/<mesh\b[\s\S]*?<\/mesh>/gi);

  for (const meshMatch of meshBlocks) {
    const meshXml = meshMatch[0];
    const sourceVertices = [];
    for (const vertexMatch of meshXml.matchAll(/<vertex\b([^>]*)\/?>/gi)) {
      const attrs = readXmlAttributes(vertexMatch[1]);
      const x = Number(attrs.x);
      const y = Number(attrs.y);
      const z = Number(attrs.z);
      if (Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(z)) {
        sourceVertices.push([x, y, z]);
      }
    }

    const baseVertex = positions.length / 3;
    for (const vertex of sourceVertices) positions.push(vertex[0], vertex[1], vertex[2]);

    for (const triangleMatch of meshXml.matchAll(/<triangle\b([^>]*)\/?>/gi)) {
      const attrs = readXmlAttributes(triangleMatch[1]);
      const a = Number.parseInt(attrs.v1, 10);
      const b = Number.parseInt(attrs.v2, 10);
      const c = Number.parseInt(attrs.v3, 10);
      if (isValid3mfVertexIndex(a, sourceVertices.length) &&
        isValid3mfVertexIndex(b, sourceVertices.length) &&
        isValid3mfVertexIndex(c, sourceVertices.length)) {
        indices.push(baseVertex + a, baseVertex + b, baseVertex + c);
      }
    }
  }

  if (positions.length === 0 || indices.length === 0) {
    throw new Error("3MF model did not contain complete triangle mesh data.");
  }
  return { positions, indices };
}

function readXmlAttributes(source) {
  const attrs = {};
  for (const match of String(source || "").matchAll(/([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) {
    attrs[match[1]] = match[2] ?? match[3] ?? "";
  }
  return attrs;
}

function isValid3mfVertexIndex(index, vertexCount) {
  return Number.isInteger(index) && index >= 0 && index < vertexCount;
}

function looksLikeGlb(bytes) {
  if (bytes.length < 20) return false;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return view.getUint32(0, true) === 0x46546c67 && view.getUint32(4, true) === 2;
}

function looksLikeGltf(bytes) {
  const prefix = new TextDecoder("utf-8").decode(bytes.slice(0, Math.min(bytes.length, 2048))).trimStart();
  if (!prefix.startsWith("{")) return false;
  return /"asset"\s*:/.test(prefix) && /"version"\s*:\s*"2\.0"/.test(prefix);
}

function parseGlb(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint32(0, true) !== 0x46546c67) {
    throw new Error("GLB magic header is invalid.");
  }
  if (view.getUint32(4, true) !== 2) {
    throw new Error("Browser advisory runtime supports GLB version 2 only.");
  }

  const totalLength = view.getUint32(8, true);
  const chunks = readGlbChunks(bytes, Math.min(totalLength, bytes.length));
  const jsonBytes = chunks.get("JSON");
  const binBytes = chunks.get("BIN");
  if (!jsonBytes || !binBytes) {
    throw new Error("GLB must include JSON and BIN chunks.");
  }

  const gltf = JSON.parse(new TextDecoder("utf-8").decode(jsonBytes).trim());
  return meshFromGltf(gltf, [binBytes], "GLB");
}

function parseGltf(bytes) {
  const gltf = JSON.parse(new TextDecoder("utf-8").decode(bytes).trim());
  const buffers = (gltf.buffers || []).map((buffer, index) => readGltfBufferBytes(buffer, index));
  return meshFromGltf(gltf, buffers, "glTF");
}

function readGltfBufferBytes(buffer, index) {
  const uri = String(buffer?.uri || "");
  if (!uri.startsWith("data:")) {
    throw new Error(`glTF buffer ${index} must be embedded as a data URI for browser-local analysis.`);
  }

  const commaIndex = uri.indexOf(",");
  if (commaIndex < 0) {
    throw new Error(`glTF buffer ${index} data URI is malformed.`);
  }

  const metadata = uri.slice(0, commaIndex).toLowerCase();
  const payload = uri.slice(commaIndex + 1);
  if (!metadata.includes(";base64")) {
    throw new Error(`glTF buffer ${index} must use base64 data URI encoding.`);
  }

  const decoded = atob(payload);
  const bytes = new Uint8Array(decoded.length);
  for (let offset = 0; offset < decoded.length; offset += 1) {
    bytes[offset] = decoded.charCodeAt(offset);
  }

  return bytes;
}

function meshFromGltf(gltf, buffers, sourceName) {
  const positions = [];
  const indices = [];

  for (const mesh of gltf.meshes || []) {
    for (const primitive of mesh.primitives || []) {
      const positionAccessorIndex = primitive.attributes?.POSITION;
      if (positionAccessorIndex === undefined) continue;
      const primitivePositions = readGltfAccessorVec3(gltf, buffers, positionAccessorIndex, sourceName);
      const primitiveIndices = primitive.indices === undefined
        ? sequentialIndices(primitivePositions.length / 3)
        : readGltfAccessorScalars(gltf, buffers, primitive.indices, sourceName);
      const baseVertex = positions.length / 3;
      // Plain loop instead of push(...spread): large GLB primitives overflow
      // the call stack when spread as arguments.
      for (const position of primitivePositions) positions.push(position);
      for (const index of primitiveIndices) indices.push(baseVertex + index);
    }
  }

  if (positions.length === 0 || indices.length === 0) {
    throw new Error(`${sourceName} did not contain mesh primitive triangle data.`);
  }
  return { positions, indices };
}

function readGlbChunks(bytes, totalLength) {
  const chunks = new Map();
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 12;
  while (offset + 8 <= totalLength) {
    const chunkLength = view.getUint32(offset, true);
    const chunkType = view.getUint32(offset + 4, true);
    offset += 8;
    if (offset + chunkLength > bytes.length) break;
    const chunkBytes = bytes.slice(offset, offset + chunkLength);
    if (chunkType === 0x4e4f534a) chunks.set("JSON", chunkBytes);
    if (chunkType === 0x004e4942) chunks.set("BIN", chunkBytes);
    offset += chunkLength;
  }
  return chunks;
}

function sequentialIndices(count) {
  return Array.from({ length: count }, (_, index) => index);
}

function readGltfAccessorVec3(gltf, buffers, accessorIndex, sourceName) {
  const accessor = gltf.accessors?.[accessorIndex];
  if (!accessor || accessor.type !== "VEC3" || accessor.componentType !== 5126) {
    throw new Error(`${sourceName} POSITION accessor must be VEC3 float32.`);
  }
  const { view, start, stride } = gltfAccessorView(gltf, buffers, accessor, sourceName);
  const values = [];
  for (let index = 0; index < accessor.count; index += 1) {
    const offset = start + index * stride;
    values.push(
      view.getFloat32(offset, true),
      view.getFloat32(offset + 4, true),
      view.getFloat32(offset + 8, true)
    );
  }
  return values;
}

function readGltfAccessorScalars(gltf, buffers, accessorIndex, sourceName) {
  const accessor = gltf.accessors?.[accessorIndex];
  if (!accessor || accessor.type !== "SCALAR") {
    throw new Error(`${sourceName} index accessor must be scalar.`);
  }
  const { view, start, stride } = gltfAccessorView(gltf, buffers, accessor, sourceName);
  const reader = glbScalarReader(view, accessor.componentType);
  const values = [];
  for (let index = 0; index < accessor.count; index += 1) {
    values.push(reader(start + index * stride));
  }
  return values;
}

function gltfAccessorView(gltf, buffers, accessor, sourceName) {
  const bufferView = gltf.bufferViews?.[accessor.bufferView];
  if (!bufferView) {
    throw new Error(`${sourceName} accessor must reference a bufferView.`);
  }
  const bufferIndex = bufferView.buffer || 0;
  const bufferBytes = buffers[bufferIndex];
  if (!bufferBytes) {
    throw new Error(`${sourceName} bufferView references missing buffer ${bufferIndex}.`);
  }
  const componentSize = glbComponentSize(accessor.componentType);
  const typeWidth = accessor.type === "VEC3" ? 3 : 1;
  return {
    view: new DataView(bufferBytes.buffer, bufferBytes.byteOffset, bufferBytes.byteLength),
    start: (bufferView.byteOffset || 0) + (accessor.byteOffset || 0),
    stride: bufferView.byteStride || componentSize * typeWidth
  };
}

function glbComponentSize(componentType) {
  if (componentType === 5120 || componentType === 5121) return 1;
  if (componentType === 5122 || componentType === 5123) return 2;
  if (componentType === 5125 || componentType === 5126) return 4;
  throw new Error(`Unsupported GLB accessor component type: ${componentType}`);
}

function glbScalarReader(view, componentType) {
  if (componentType === 5121) return offset => view.getUint8(offset);
  if (componentType === 5123) return offset => view.getUint16(offset, true);
  if (componentType === 5125) return offset => view.getUint32(offset, true);
  throw new Error("GLB indices must use unsigned byte, unsigned short, or unsigned int components.");
}

// Welds vertices that share the same position (quantized to 1 µm) so edge and
// body analysis sees real mesh topology. Viewer meshes (STL parsing, GLB
// tessellation) duplicate vertices per face — counting edges on raw indices
// makes every edge look like a boundary and reports thousands of bogus
// "non-manifold" edges on perfectly valid parts.
function buildWeldedIndexMap(positions, sourceGroups = null) {
  const canonicalByKey = new Map();
  const vertexCount = positions.length / 3;
  const weldedIndex = new Array(vertexCount);
  const groupByVertex = buildSourceGroupLookup(vertexCount, sourceGroups);
  for (let vertex = 0; vertex < vertexCount; vertex += 1) {
    const key = `${groupByVertex[vertex]}:` +
      `${Math.round(positions[vertex * 3] * 1000)}:` +
      `${Math.round(positions[vertex * 3 + 1] * 1000)}:` +
      `${Math.round(positions[vertex * 3 + 2] * 1000)}`;
    let canonical = canonicalByKey.get(key);
    if (canonical === undefined) {
      canonical = vertex;
      canonicalByKey.set(key, canonical);
    }
    weldedIndex[vertex] = canonical;
  }
  return weldedIndex;
}

function buildSourceGroupLookup(vertexCount, sourceGroups) {
  const groupByVertex = new Array(vertexCount).fill(0);
  if (!Array.isArray(sourceGroups) || sourceGroups.length <= 1) return groupByVertex;

  for (let groupIndex = 0; groupIndex < sourceGroups.length; groupIndex += 1) {
    const group = sourceGroups[groupIndex] || {};
    const start = Math.max(0, Math.trunc(Number(group.startVertex) || 0));
    const end = Math.min(vertexCount, Math.trunc(Number(group.endVertex) || 0));
    for (let vertex = start; vertex < end; vertex += 1) {
      groupByVertex[vertex] = groupIndex;
    }
  }
  return groupByVertex;
}

function createUnionFind() {
  const parent = new Map();
  function find(value) {
    let root = value;
    while (parent.has(root) && parent.get(root) !== root) root = parent.get(root);
    // Path compression
    let cursor = value;
    while (parent.has(cursor) && parent.get(cursor) !== root) {
      const next = parent.get(cursor);
      parent.set(cursor, root);
      cursor = next;
    }
    if (!parent.has(value)) parent.set(value, root);
    return root;
  }
  function union(a, b) {
    const rootA = find(a);
    const rootB = find(b);
    if (rootA !== rootB) parent.set(rootA, rootB);
  }
  return { find, union, parent };
}

// Re-orients triangle normals so adjacent faces agree on winding within each
// connected surface (mirrors GeometryService's trimesh.repair.fix_winding).
// A handful of inconsistently wound faces — most likely right at a
// tessellation seam, e.g. where an open cavity's rim meets the outer wall —
// report their normals on the wrong side of the overhang threshold, which
// looks like the whole check is backwards even though the threshold itself
// is fine. Mutates triangles[].normal in place.
function repairTriangleWinding(triangles, weldedTriangles) {
  const edgeFaces = new Map();
  for (let face = 0; face < weldedTriangles.length; face += 1) {
    const [a, b, c] = weldedTriangles[face];
    for (const [from, to] of [[a, b], [b, c], [c, a]]) {
      if (from === to) continue;
      const key = from < to ? `${from}:${to}` : `${to}:${from}`;
      const dir = from < to ? 1 : -1;
      let entries = edgeFaces.get(key);
      if (!entries) { entries = []; edgeFaces.set(key, entries); }
      entries.push({ face, dir });
    }
  }

  const adjacency = new Map();
  for (const entries of edgeFaces.values()) {
    for (let i = 0; i < entries.length; i += 1) {
      for (let j = 0; j < entries.length; j += 1) {
        if (i === j) continue;
        const from = entries[i];
        const to = entries[j];
        let list = adjacency.get(from.face);
        if (!list) { list = []; adjacency.set(from.face, list); }
        list.push({ neighbor: to.face, sameDirection: from.dir === to.dir });
      }
    }
  }

  const flipped = new Set();
  const visited = new Set();
  for (let start = 0; start < weldedTriangles.length; start += 1) {
    if (visited.has(start)) continue;
    visited.add(start);
    const queue = [start];
    while (queue.length > 0) {
      const current = queue.pop();
      const currentFlipped = flipped.has(current);
      for (const { neighbor, sameDirection } of adjacency.get(current) ?? []) {
        if (visited.has(neighbor)) continue;
        visited.add(neighbor);
        // Properly wound adjacent triangles traverse a shared edge in
        // OPPOSITE directions. Traversing it in the same direction means
        // exactly one side of the pair is wound backwards.
        if (sameDirection ? !currentFlipped : currentFlipped) flipped.add(neighbor);
        queue.push(neighbor);
      }
    }
  }

  for (const face of flipped) {
    const normal = triangles[face].normal;
    normal[0] = -normal[0];
    normal[1] = -normal[1];
    normal[2] = -normal[2];
  }
}

function computeMetrics(mesh, runtimeKernel = null) {
  const { positions, indices } = mesh;
  if (positions.length === 0 || indices.length === 0) {
    return emptyMetrics();
  }

  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (let index = 0; index < positions.length; index += 3) {
    for (let axis = 0; axis < 3; axis += 1) {
      const value = positions[index + axis];
      if (value < min[axis]) min[axis] = value;
      if (value > max[axis]) max[axis] = value;
    }
  }

  const weldedIndex = buildWeldedIndexMap(positions, mesh.sourceGroups);
  const bodies = createUnionFind();

  let area = 0;
  let signedVolume = 0;
  const edgeCounts = new Map();
  const triangles = [];
  const weldedTriangles = [];
  const minZ = min[2];
  const zTolerance = Math.max(0.01, (max[2] - min[2]) * 0.001);

  for (let index = 0; index < indices.length; index += 3) {
    const faceIndex = index / 3;
    const ia = indices[index] * 3;
    const ib = indices[index + 1] * 3;
    const ic = indices[index + 2] * 3;
    const a = [positions[ia], positions[ia + 1], positions[ia + 2]];
    const b = [positions[ib], positions[ib + 1], positions[ib + 2]];
    const c = [positions[ic], positions[ic + 1], positions[ic + 2]];
    const ab = subtract(b, a);
    const ac = subtract(c, a);
    const cross = crossProduct(ab, ac);
    const crossLength = vectorLength(cross);
    const faceArea = crossLength / 2;
    const normal = crossLength > 0
      ? [cross[0] / crossLength, cross[1] / crossLength, cross[2] / crossLength]
      : [0, 0, 0];
    const centroid = [
      (a[0] + b[0] + c[0]) / 3,
      (a[1] + b[1] + c[1]) / 3,
      (a[2] + b[2] + c[2]) / 3
    ];
    const touchesBuildPlate = a[2] <= minZ + zTolerance
      && b[2] <= minZ + zTolerance
      && c[2] <= minZ + zTolerance;

    area += faceArea;
    signedVolume += dot(a, crossProduct(b, c)) / 6;

    const wa = weldedIndex[indices[index]];
    const wb = weldedIndex[indices[index + 1]];
    const wc = weldedIndex[indices[index + 2]];
    weldedTriangles.push([wa, wb, wc]);
    bodies.union(wa, wb);
    bodies.union(wb, wc);
    // Skip degenerate triangles (welded duplicates) for edge topology — their
    // zero-length edges would distort the manifold classification.
    if (wa !== wb && wb !== wc && wc !== wa) {
      countEdge(edgeCounts, wa, wb);
      countEdge(edgeCounts, wb, wc);
      countEdge(edgeCounts, wc, wa);
    }

    triangles.push({
      faceIndex,
      areaMm2: faceArea,
      normal,
      centroid,
      touchesBuildPlate
    });
  }

  repairTriangleWinding(triangles, weldedTriangles);

  let openEdgeCount = 0;
  let nonManifoldEdgeCount = 0;
  for (const count of edgeCounts.values()) {
    if (count === 1) openEdgeCount += 1;
    else if (count > 2) nonManifoldEdgeCount += 1;
  }

  const bodyRoots = new Set();
  for (const [wa] of weldedTriangles) bodyRoots.add(bodies.find(wa));

  const extents = [max[0] - min[0], max[1] - min[1], max[2] - min[2]];
  const faceCount = computeTriangleCount(indices.length, runtimeKernel);

  return {
    vertexCount: positions.length / 3,
    faceCount,
    volumeMm3: Math.abs(signedVolume),
    surfaceAreaMm2: area,
    boundingBox: { x: extents[0], y: extents[1], z: extents[2] },
    isManifold: nonManifoldEdgeCount === 0 && openEdgeCount === 0,
    nonManifoldEdgeCount,
    openEdgeCount,
    bodyCount: bodyRoots.size,
    triangles,
    weldedIndices: weldedTriangles,
    isEmpty: indices.length === 0,
    complexity: complexityFor(faceCount)
  };
}

function computeTriangleCount(indexCount, runtimeKernel = null) {
  const wasmTriangleCounter = runtimeKernel?.exports?.triangle_count_from_indices;
  if (typeof wasmTriangleCounter === "function") {
    const count = Number(wasmTriangleCounter(Math.max(0, Math.trunc(indexCount))));
    if (Number.isFinite(count) && count >= 0) return count;
  }
  return indexCount / 3;
}

function emptyMetrics() {
  return {
    vertexCount: 0,
    faceCount: 0,
    volumeMm3: 0,
    surfaceAreaMm2: 0,
    boundingBox: { x: 0, y: 0, z: 0 },
    isManifold: false,
    nonManifoldEdgeCount: 0,
    openEdgeCount: 0,
    bodyCount: 0,
    triangles: [],
    weldedIndices: [],
    isEmpty: true,
    complexity: "empty"
  };
}

// Mesh-integrity issues shared by analyze and compute_metrics — independent of
// the manufacturing process.
function buildIntegrityIssues(metrics) {
  const issues = [];
  if (metrics.isEmpty) {
    issues.push(issue("system", "error", "Empty mesh", "No triangle geometry was available for local advisory analysis.", 0, 1));
    return issues;
  }
  if (metrics.nonManifoldEdgeCount > 0) {
    issues.push(issue(
      "mesh_integrity",
      "warning",
      "Non-manifold mesh",
      `Found ${metrics.nonManifoldEdgeCount.toLocaleString()} non-manifold edge(s) shared by more than two faces. This may cause problems during manufacturing.`,
      metrics.nonManifoldEdgeCount,
      0));
  } else if (metrics.openEdgeCount > 0) {
    issues.push(issue(
      "mesh_integrity",
      "warning",
      "Open mesh edges",
      `Found ${metrics.openEdgeCount.toLocaleString()} open edge(s) — the mesh is not watertight and may need repair before manufacturing.`,
      metrics.openEdgeCount,
      0));
  }
  if ((metrics.bodyCount ?? 1) > 1) {
    issues.push(issue(
      "multi_body",
      "info",
      "Multiple bodies",
      `The model contains ${metrics.bodyCount.toLocaleString()} separate bodies. Each body is manufactured as its own part.`,
      metrics.bodyCount,
      1));
  }
  const boundingBox = metrics.boundingBox || { x: 0, y: 0, z: 0 };
  const maxExtentMm = Math.max(boundingBox.x, boundingBox.y, boundingBox.z);
  if (maxExtentMm > 0 && maxExtentMm < 1.0) {
    issues.push(issue(
      "part_size",
      "warning",
      "Sub-millimeter part",
      `The largest dimension is ${maxExtentMm.toFixed(3)} mm. This usually means the file was exported in the wrong units (meters or inches instead of millimeters) — verify the source units before quoting.`,
      maxExtentMm,
      1.0));
  }
  return issues;
}

// Overhang faces need support when they face downward more steeply than the
// standard 45° self-supporting limit: normal Z below -cos(45°) in Z-up space.
const OVERHANG_NORMAL_Z_LIMIT = -Math.SQRT1_2;

// Groups overhang faces into connected regions via shared welded mesh edges so
// the report can say "3 overhang regions" instead of a raw triangle count.
function groupOverhangRegions(overhangFaces, weldedIndices) {
  if (!Array.isArray(weldedIndices) || weldedIndices.length === 0) {
    return overhangFaces.length > 0 ? 1 : 0;
  }
  const regions = createUnionFind();
  const facesByEdge = new Map();
  for (const face of overhangFaces) {
    const welded = weldedIndices[face.faceIndex];
    if (!welded) continue;
    regions.find(face.faceIndex);
    const [wa, wb, wc] = welded;
    for (const [lo, hi] of [[wa, wb], [wb, wc], [wc, wa]]) {
      const key = lo < hi ? `${lo}:${hi}` : `${hi}:${lo}`;
      const neighbour = facesByEdge.get(key);
      if (neighbour === undefined) facesByEdge.set(key, face.faceIndex);
      else regions.union(neighbour, face.faceIndex);
    }
  }
  const roots = new Set();
  for (const face of overhangFaces) roots.add(regions.find(face.faceIndex));
  return roots.size;
}

function buildIssues(metrics, processCode) {
  const issues = buildIntegrityIssues(metrics);
  if (metrics.isEmpty) {
    return issues;
  }

  const minExtent = Math.min(metrics.boundingBox.x, metrics.boundingBox.y, metrics.boundingBox.z);
  const normalizedProcess = String(processCode).toUpperCase();
  const printing = ["FDM", "SLA", "SLS", "MJF", "MJ", "BJ", "DMLS", "SLA_DLP", "DLP"].includes(normalizedProcess);
  if (printing && minExtent > 0 && minExtent < 0.8) {
    issues.push(issue("thin_wall", "warning", "Thin feature risk", "One model dimension is below the 0.8 mm local advisory threshold.", minExtent, 0.8));
  }

  const supportProcesses = ["FDM", "SLA", "SLA_DLP", "DLP"];
  if (supportProcesses.includes(normalizedProcess)) {
    const overhangFaces = metrics.triangles
      .filter(triangle => !triangle.touchesBuildPlate
        && triangle.normal[2] < OVERHANG_NORMAL_Z_LIMIT);
    if (overhangFaces.length > 0) {
      const overhangAreaMm2 = overhangFaces.reduce((sum, triangle) => sum + triangle.areaMm2, 0);
      const regionCount = groupOverhangRegions(overhangFaces, metrics.weldedIndices);
      issues.push(issue(
        "overhang",
        "warning",
        "Local support risk",
        `Found ${regionCount.toLocaleString()} overhang region(s) (≈${Math.round(overhangAreaMm2).toLocaleString()} mm² total) steeper than 45° that may require supports.`,
        regionCount,
        0,
        overhangFaces.map(triangle => triangle.faceIndex),
        averageCentroid(overhangFaces)
      ));
    }
  }

  return issues;
}

function issue(category, severity, title, description, value, threshold, faceIndices = [], centroid = []) {
  return { category, severity, title, description, value, threshold, faceIndices, centroid, source: "local" };
}

function buildLocalOverlayHints(issues) {
  return issues.map(issue => ({
    category: issue.category,
    severity: issue.severity,
    title: issue.title,
    faceIndices: issue.faceIndices || [],
    centroid: issue.centroid || []
  }));
}

function complexityFor(faceCount) {
  if (faceCount < 1000) return "simple";
  if (faceCount < 20000) return "medium";
  return "complex";
}

function countEdge(map, a, b) {
  const lo = Math.min(a, b);
  const hi = Math.max(a, b);
  const key = `${lo}:${hi}`;
  map.set(key, (map.get(key) || 0) + 1);
}

function subtract(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

function crossProduct(a, b) {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0]
  ];
}

function dot(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

function vectorLength(v) {
  return Math.hypot(v[0], v[1], v[2]);
}

function averageCentroid(triangles) {
  if (triangles.length === 0) return [];
  const total = triangles.reduce((sum, triangle) => [
    sum[0] + triangle.centroid[0],
    sum[1] + triangle.centroid[1],
    sum[2] + triangle.centroid[2]
  ], [0, 0, 0]);
  return total.map(value => value / triangles.length);
}

async function hashMesh(mesh) {
  const bytes = new TextEncoder().encode(JSON.stringify({
    positions: mesh.positions.slice(0, 300000),
    indices: mesh.indices.slice(0, 300000),
    totalPositionValues: mesh.positions.length,
    totalIndexValues: mesh.indices.length
  }));
  if (self.crypto?.subtle) {
    const digest = await self.crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, "0")).join("");
  }
  let hash = 2166136261;
  for (const byte of bytes) {
    hash ^= byte;
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}
