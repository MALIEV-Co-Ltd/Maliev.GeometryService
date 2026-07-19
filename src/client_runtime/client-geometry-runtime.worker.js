const MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION = "2.0.0";
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
  const issues = buildIssues(mesh, metrics, processCode);
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

// ============================================================================
// DFM ENGINE — full client-side design-for-manufacturing screening.
// Ported from the GeometryService Python reference detectors (mesh_analyzers /
// cnc_analyzers / dfm_thresholds) so every process family runs entirely in the
// browser: the server performs NO analysis. Every issue carries faceIndices +
// centroid so the viewer overlays the exact affected region on the mesh.
// ============================================================================

// --- Process design rules (mirror of src/core/dfm_thresholds.py) -----------

const PRINTING_RULES = {
  FDM: {
    supportedWallMm: 0.8, unsupportedWallMm: 0.8, maxOverhangDeg: 45,
    embossedWidthMm: 0.6, embossedHeightMm: 2.0, bridgeSpanMm: 10.0,
    minHoleDiameterMm: 2.0, connectingClearanceMm: 0.5, escapeHoleDiameterMm: null,
    minFeatureMm: 2.0, pinDiameterMm: 3.0, firstLayerThicknessMm: 0.3
  },
  SLA: {
    supportedWallMm: 0.5, unsupportedWallMm: 1.0, maxOverhangDeg: null,
    embossedWidthMm: 0.4, embossedHeightMm: 0.4, bridgeSpanMm: null,
    minHoleDiameterMm: 0.5, connectingClearanceMm: 0.5, escapeHoleDiameterMm: 4.0,
    minFeatureMm: 0.2, pinDiameterMm: 0.5, firstLayerThicknessMm: 0.1
  },
  SLS: {
    supportedWallMm: 0.7, unsupportedWallMm: null, maxOverhangDeg: null,
    embossedWidthMm: 1.0, embossedHeightMm: 1.0, bridgeSpanMm: null,
    minHoleDiameterMm: 1.5, connectingClearanceMm: 0.3, escapeHoleDiameterMm: 5.0,
    minFeatureMm: 0.8, pinDiameterMm: 0.8, firstLayerThicknessMm: 0.15
  },
  MJ: {
    supportedWallMm: 1.0, unsupportedWallMm: 1.0, maxOverhangDeg: null,
    embossedWidthMm: 0.5, embossedHeightMm: 0.5, bridgeSpanMm: null,
    minHoleDiameterMm: 0.5, connectingClearanceMm: 0.2, escapeHoleDiameterMm: null,
    minFeatureMm: 0.5, pinDiameterMm: 0.5, firstLayerThicknessMm: 0.3
  },
  BJ: {
    supportedWallMm: 2.0, unsupportedWallMm: 3.0, maxOverhangDeg: null,
    embossedWidthMm: 0.5, embossedHeightMm: 0.5, bridgeSpanMm: null,
    minHoleDiameterMm: 1.5, connectingClearanceMm: null, escapeHoleDiameterMm: 5.0,
    minFeatureMm: 2.0, pinDiameterMm: 2.0, firstLayerThicknessMm: 0.3
  },
  DMLS: {
    supportedWallMm: 0.4, unsupportedWallMm: 0.5, maxOverhangDeg: null,
    embossedWidthMm: 0.1, embossedHeightMm: 0.1, bridgeSpanMm: 2.0,
    minHoleDiameterMm: 1.5, connectingClearanceMm: null, escapeHoleDiameterMm: 5.0,
    minFeatureMm: 0.6, pinDiameterMm: 1.0, firstLayerThicknessMm: 0.3
  }
};
PRINTING_RULES.SLA_DLP = PRINTING_RULES.SLA;
PRINTING_RULES.DLP = PRINTING_RULES.SLA;
PRINTING_RULES.MJF = PRINTING_RULES.SLS;

// Powder-bed processes self-support: no overhang/bridge concerns (parity with
// the Python analyzer's powder_bed_processes skip list).
const POWDER_BED_PROCESSES = new Set(["SLS", "MJF", "BJ", "DMLS"]);
// Resin processes trap uncured liquid in enclosed volumes.
const RESIN_PROCESSES = new Set(["SLA", "SLA_DLP", "DLP"]);

const MILLING_RULES = {
  minInternalRadiusMm: 1.0,
  cavityDepthRatio: 4.0,
  holeDepthTypicalRatio: 10.0,
  holeDepthFeasibleRatio: 40.0,
  chatterMinAreaCm2: 4.0,
  chatterMaxThicknessMm: 3.0,
  sharpCornerThresholdDeg: 45.0
};

// (tool_diameter_mm, standard_length_mm, max_length_mm, min_corner_radius_mm)
const TOOL_DIAMETER_TABLE = [
  [2.0, 8.0, 10.0, 1.5], [3.0, 12.0, 15.0, 2.0], [4.0, 15.0, 20.0, 2.5],
  [6.0, 25.0, 30.0, 3.5], [8.0, 35.0, 40.0, 4.5], [10.0, 45.0, 50.0, 5.5],
  [12.0, 55.0, 60.0, 6.5], [16.0, 75.0, 80.0, 8.5], [20.0, 95.0, 100.0, 10.5],
  [25.0, 120.0, 125.0, 13.0], [32.0, 155.0, 160.0, 17.0],
  [50.0, 240.0, 250.0, 27.0], [63.0, 305.0, 315.0, 35.0]
];

function toolForRadius(radiusMm) {
  for (const [diameter, stdLen, maxLen, minRadius] of TOOL_DIAMETER_TABLE) {
    if (radiusMm >= minRadius) return { diameter, stdLen, maxLen };
  }
  return null;
}

const TURNING_RULES = {
  maxLengthDiameterRatio: 8.0,
  minGrooveWidthMm: 3.0,
  symmetryThreshold: 0.15
};

// Sheet metal advisory rules: bend radius ≥ 1× thickness, hole Ø ≥ 1× thickness.
const SHEET_METAL_RULES = {
  minThicknessMm: 0.3,
  maxThicknessMm: 6.0,
  minBendRadiusFactor: 1.0,
  minHoleDiameterFactor: 1.0,
  uniformityCoverage: 0.5
};

// Silicone / vacuum casting advisory rules.
const SILICONE_RULES = {
  minWallMm: 0.5,
  tearSlotDepthRatio: 2.0,
  tearSlotMaxWidthMm: 3.0
};

// Warpage / surface-defect advisory heuristics (FDM-family).
const WARPAGE_ASPECT_THRESHOLD = 20.0;
const WARPAGE_MIN_SPAN_MM = 75.0;
const WARPAGE_MAX_THICKNESS_MM = 4.0;
const STAIRCASE_MIN_AREA_MM2 = 100.0;

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

// --- Shared geometry context -------------------------------------------------
// Built once per analyze() call; all detectors read from it.

function buildDfmContext(mesh, metrics) {
  const { positions, indices } = mesh;
  const faceCount = metrics.weldedIndices.length;
  const tris = metrics.triangles;

  // Welded edge -> adjacent faces, and face-level adjacency.
  const edgeFaces = new Map();
  for (let f = 0; f < faceCount; f += 1) {
    const welded = metrics.weldedIndices[f];
    const [wa, wb, wc] = welded;
    if (wa === wb || wb === wc || wc === wa) continue;
    for (const [a, b] of [[wa, wb], [wb, wc], [wc, wa]]) {
      const key = a < b ? `${a}:${b}` : `${b}:${a}`;
      let list = edgeFaces.get(key);
      if (!list) { list = []; edgeFaces.set(key, list); }
      list.push(f);
    }
  }
  const adjacency = Array.from({ length: faceCount }, () => []);
  for (const faces of edgeFaces.values()) {
    for (let i = 0; i < faces.length; i += 1) {
      for (let j = i + 1; j < faces.length; j += 1) {
        adjacency[faces[i]].push(faces[j]);
        adjacency[faces[j]].push(faces[i]);
      }
    }
  }

  const faceVertexPositions = f => {
    const i3 = f * 3;
    const out = [];
    for (let k = 0; k < 3; k += 1) {
      const v = indices[i3 + k] * 3;
      out.push([positions[v], positions[v + 1], positions[v + 2]]);
    }
    return out;
  };

  // Connected bodies: faces grouped by welded-vertex union-find root.
  const uf = createUnionFind();
  for (let f = 0; f < faceCount; f += 1) {
    const [wa, wb, wc] = metrics.weldedIndices[f];
    uf.union(wa, wb);
    uf.union(wb, wc);
  }
  const bodyFaces = new Map();
  for (let f = 0; f < faceCount; f += 1) {
    const root = uf.find(metrics.weldedIndices[f][0]);
    let list = bodyFaces.get(root);
    if (!list) { list = []; bodyFaces.set(root, list); }
    list.push(f);
  }
  const bodies = [];
  for (const faces of bodyFaces.values()) {
    const min = [Infinity, Infinity, Infinity];
    const max = [-Infinity, -Infinity, -Infinity];
    for (const f of faces) {
      for (const p of faceVertexPositions(f)) {
        for (let axis = 0; axis < 3; axis += 1) {
          if (p[axis] < min[axis]) min[axis] = p[axis];
          if (p[axis] > max[axis]) max[axis] = p[axis];
        }
      }
    }
    const centroid = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
    bodies.push({ faces, min, max, centroid });
  }

  const context = {
    mesh, metrics, faceCount, tris, edgeFaces, adjacency,
    faceVertexPositions, bodies,
    minZ: null, maxZ: null, cylinders: null, xyGrid: null
  };

  let minZ = Infinity;
  let maxZ = -Infinity;
  for (let i = 2; i < positions.length; i += 3) {
    if (positions[i] < minZ) minZ = positions[i];
    if (positions[i] > maxZ) maxZ = positions[i];
  }
  context.minZ = minZ;
  context.maxZ = maxZ;
  return context;
}

// BFS clustering of a face set through shared welded edges.
function clusterFaceSet(context, faceSet) {
  const clusters = [];
  const visited = new Set();
  for (const start of faceSet) {
    if (visited.has(start)) continue;
    const cluster = [];
    const queue = [start];
    while (queue.length > 0) {
      const current = queue.pop();
      if (visited.has(current)) continue;
      visited.add(current);
      cluster.push(current);
      for (const next of context.adjacency[current]) {
        if (!visited.has(next) && faceSet.has(next)) queue.push(next);
      }
    }
    clusters.push(cluster);
  }
  return clusters;
}

function clusterBounds(context, cluster) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (const f of cluster) {
    for (const p of context.faceVertexPositions(f)) {
      for (let axis = 0; axis < 3; axis += 1) {
        if (p[axis] < min[axis]) min[axis] = p[axis];
        if (p[axis] > max[axis]) max[axis] = p[axis];
      }
    }
  }
  return { min, max };
}

function clusterCentroid(context, cluster) {
  const total = [0, 0, 0];
  for (const f of cluster) {
    const c = context.tris[f].centroid;
    total[0] += c[0]; total[1] += c[1]; total[2] += c[2];
  }
  return total.map(v => v / Math.max(1, cluster.length));
}

// Spatial pairing: invoke cb(i, j) for face-centroid pairs within radius.
function forEachClosePair(context, radius, cb) {
  const cell = Math.max(radius, 1e-6);
  const grid = new Map();
  const keyFor = c => `${Math.floor(c[0] / cell)}:${Math.floor(c[1] / cell)}:${Math.floor(c[2] / cell)}`;
  for (let f = 0; f < context.faceCount; f += 1) {
    const key = keyFor(context.tris[f].centroid);
    let list = grid.get(key);
    if (!list) { list = []; grid.set(key, list); }
    list.push(f);
  }
  const radiusSq = radius * radius;
  for (let f = 0; f < context.faceCount; f += 1) {
    const c = context.tris[f].centroid;
    const gx = Math.floor(c[0] / cell);
    const gy = Math.floor(c[1] / cell);
    const gz = Math.floor(c[2] / cell);
    for (let dx = -1; dx <= 1; dx += 1) {
      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dz = -1; dz <= 1; dz += 1) {
          const list = grid.get(`${gx + dx}:${gy + dy}:${gz + dz}`);
          if (!list) continue;
          for (const g of list) {
            if (g <= f) continue;
            const o = context.tris[g].centroid;
            const ddx = o[0] - c[0];
            const ddy = o[1] - c[1];
            const ddz = o[2] - c[2];
            if (ddx * ddx + ddy * ddy + ddz * ddz <= radiusSq) cb(f, g);
          }
        }
      }
    }
  }
}

// Vertical (±Z) ray casting via an XY triangle grid. Only non-vertical faces
// participate (vertical faces have a degenerate XY footprint). Returns sorted
// hit list [{ z, faceIndex }] for the vertical line through (x, y).
function ensureXyGrid(context) {
  if (context.xyGrid) return context.xyGrid;
  const extent = Math.max(context.metrics.boundingBox.x, context.metrics.boundingBox.y, 1e-6);
  const cell = Math.max(extent / 64, 0.5);
  const grid = new Map();
  for (let f = 0; f < context.faceCount; f += 1) {
    if (Math.abs(context.tris[f].normal[2]) < 1e-6) continue;
    const [a, b, c] = context.faceVertexPositions(f);
    const minX = Math.min(a[0], b[0], c[0]);
    const maxX = Math.max(a[0], b[0], c[0]);
    const minY = Math.min(a[1], b[1], c[1]);
    const maxY = Math.max(a[1], b[1], c[1]);
    for (let gx = Math.floor(minX / cell); gx <= Math.floor(maxX / cell); gx += 1) {
      for (let gy = Math.floor(minY / cell); gy <= Math.floor(maxY / cell); gy += 1) {
        const key = `${gx}:${gy}`;
        let list = grid.get(key);
        if (!list) { list = []; grid.set(key, list); }
        list.push(f);
      }
    }
  }
  context.xyGrid = { grid, cell };
  return context.xyGrid;
}

function verticalHits(context, x, y, excludeFace = -1) {
  const { grid, cell } = ensureXyGrid(context);
  const list = grid.get(`${Math.floor(x / cell)}:${Math.floor(y / cell)}`);
  if (!list) return [];
  const hits = [];
  for (const f of list) {
    if (f === excludeFace) continue;
    const [a, b, c] = context.faceVertexPositions(f);
    const d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1]);
    if (Math.abs(d) < 1e-12) continue;
    const w0 = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / d;
    const w1 = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / d;
    const w2 = 1 - w0 - w1;
    if (w0 < -1e-6 || w1 < -1e-6 || w2 < -1e-6) continue;
    hits.push({ z: w0 * a[2] + w1 * b[2] + w2 * c[2], faceIndex: f });
  }
  hits.sort((p, q) => p.z - q.z);
  return hits;
}

// Cylindrical feature detection (port of detect_cylindrical_features):
// clusters curved face adjacency whose normal-cross axes agree, fits the axis
// and radius, measures angular coverage, classifies concave (hole) vs convex
// (pin/boss).
function detectCylinders(context) {
  if (context.cylinders) return context.cylinders;
  const results = [];
  const tris = context.tris;

  // Flat patches: faces joined by coplanar adjacency. Flat-shaded quad
  // tessellations (typical STL exports) split each facet into two coplanar
  // triangles, so curved pairs on neighbouring facets never share a raw face
  // — they DO share a flat patch. Cluster curved pairs through patches.
  const patchUf = createUnionFind();
  const pairs = []; // { i, j, axis }
  for (const faces of context.edgeFaces.values()) {
    if (faces.length !== 2) continue;
    const [i, j] = faces;
    const cross = crossProduct(tris[i].normal, tris[j].normal);
    const len = vectorLength(cross);
    if (len < 5e-3) {
      patchUf.union(i, j); // coplanar neighbours form one flat patch
      continue;
    }
    pairs.push({ i, j, axis: [cross[0] / len, cross[1] / len, cross[2] / len] });
  }
  if (pairs.length === 0) {
    context.cylinders = results;
    return results;
  }

  const patchPairs = new Map(); // patch root -> pair indices touching it
  for (let p = 0; p < pairs.length; p += 1) {
    for (const f of [pairs[p].i, pairs[p].j]) {
      const root = patchUf.find(f);
      let list = patchPairs.get(root);
      if (!list) { list = []; patchPairs.set(root, list); }
      list.push(p);
    }
  }

  // Cluster PAIRS (not faces) by axis agreement through shared flat patches.
  // Order-independent: a face/patch may participate in several axis groups
  // (a cylinder side facet has axis-aligned side pairs AND tangential cap
  // pairs); only the group that actually fits a cylinder survives evaluation.
  const uf = createUnionFind();
  for (const pairIdxList of patchPairs.values()) {
    for (let a = 0; a < pairIdxList.length; a += 1) {
      for (let b = a + 1; b < pairIdxList.length; b += 1) {
        const pa = pairs[pairIdxList[a]];
        const pb = pairs[pairIdxList[b]];
        if (Math.abs(dot(pa.axis, pb.axis)) >= 0.9) {
          uf.union(pairIdxList[a], pairIdxList[b]);
        }
      }
    }
  }
  const groups = new Map(); // root -> Set(face)
  const groupAxis = new Map(); // root -> accumulated axis
  for (let p = 0; p < pairs.length; p += 1) {
    const root = uf.find(p);
    let faceSet = groups.get(root);
    if (!faceSet) { faceSet = new Set(); groups.set(root, faceSet); }
    faceSet.add(pairs[p].i);
    faceSet.add(pairs[p].j);
    const ref = groupAxis.get(root);
    if (!ref) {
      groupAxis.set(root, pairs[p].axis.slice());
    } else {
      const sign = dot(pairs[p].axis, ref) >= 0 ? 1 : -1;
      ref[0] += sign * pairs[p].axis[0];
      ref[1] += sign * pairs[p].axis[1];
      ref[2] += sign * pairs[p].axis[2];
    }
  }

  for (const [root, faceSet] of groups.entries()) {
    const cluster = Array.from(faceSet);
    const refAxis = groupAxis.get(root);
    if (cluster.length < 6 || !refAxis) continue;

    // The group axis is the sign-aligned sum of its pair axes; normalize it,
    // then verify all face normals are perpendicular to it (cones and blends
    // fail this test).
    const accLen = vectorLength(refAxis);
    if (accLen < 1e-9) continue;
    const axis = [refAxis[0] / accLen, refAxis[1] / accLen, refAxis[2] / accLen];
    let maxAlign = 0;
    for (const f of cluster) {
      maxAlign = Math.max(maxAlign, Math.abs(dot(tris[f].normal, axis)));
    }
    if (maxAlign > 0.2) continue;

    // Project cluster vertices onto the plane perpendicular to the axis.
    const tmp = Math.abs(axis[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0];
    let u = crossProduct(axis, tmp);
    const uLen = vectorLength(u);
    u = [u[0] / uLen, u[1] / uLen, u[2] / uLen];
    const v = crossProduct(axis, u);

    const seen = new Set();
    const xs = []; const ys = []; const ts = [];
    for (const f of cluster) {
      const i3 = f * 3;
      for (let k = 0; k < 3; k += 1) {
        const vi = context.mesh.indices[i3 + k];
        if (seen.has(vi)) continue;
        seen.add(vi);
        const p = [context.mesh.positions[vi * 3], context.mesh.positions[vi * 3 + 1], context.mesh.positions[vi * 3 + 2]];
        xs.push(dot(p, u));
        ys.push(dot(p, v));
        ts.push(dot(p, axis));
      }
    }
    if (xs.length < 6) continue;

    // Kåsa circle fit: solve [2x 2y 1][cx cy c0]ᵀ = x²+y² (normal equations).
    let sxx = 0; let sxy = 0; let sx1 = 0; let syy = 0; let sy1 = 0; let s11 = 0;
    let bx = 0; let by = 0; let b1 = 0;
    for (let n = 0; n < xs.length; n += 1) {
      const x2 = 2 * xs[n]; const y2 = 2 * ys[n]; const rhs = xs[n] * xs[n] + ys[n] * ys[n];
      sxx += x2 * x2; sxy += x2 * y2; sx1 += x2;
      syy += y2 * y2; sy1 += y2; s11 += 1;
      bx += x2 * rhs; by += y2 * rhs; b1 += rhs;
    }
    const sol = solve3([[sxx, sxy, sx1], [sxy, syy, sy1], [sx1, sy1, s11]], [bx, by, b1]);
    if (!sol) continue;
    const [cx, cy, c0] = sol;
    const rSq = c0 + cx * cx + cy * cy;
    if (rSq <= 0) continue;
    const radius = Math.sqrt(rSq);
    if (radius < 1e-6) continue;
    let residSum = 0; let residSqSum = 0;
    for (let n = 0; n < xs.length; n += 1) {
      const r = Math.hypot(xs[n] - cx, ys[n] - cy);
      residSum += r; residSqSum += r * r;
    }
    const meanR = residSum / xs.length;
    const stdR = Math.sqrt(Math.max(0, residSqSum / xs.length - meanR * meanR));
    if (stdR / radius > 0.08) continue;

    // Angular coverage from face centroids about the fitted center.
    const angles = cluster
      .map(f => Math.atan2(dot(tris[f].centroid, v) - cy, dot(tris[f].centroid, u) - cx))
      .sort((p, q) => p - q);
    if (angles.length < 3) continue;
    let maxGap = 2 * Math.PI - (angles[angles.length - 1] - angles[0]);
    for (let n = 1; n < angles.length; n += 1) maxGap = Math.max(maxGap, angles[n] - angles[n - 1]);
    const coverage = 2 * Math.PI - maxGap;

    // Concave when normals point toward the axis (hole); convex = pin/boss.
    let towardCount = 0;
    for (const f of cluster) {
      const fx = dot(tris[f].centroid, u) - cx;
      const fy = dot(tris[f].centroid, v) - cy;
      const nx = dot(tris[f].normal, u);
      const ny = dot(tris[f].normal, v);
      if (fx * nx + fy * ny < 0) towardCount += 1;
    }
    const concave = towardCount * 2 > cluster.length;

    let tMin = Infinity; let tMax = -Infinity;
    for (const t of ts) { if (t < tMin) tMin = t; if (t > tMax) tMax = t; }
    const mid = (tMin + tMax) / 2;
    results.push({
      center: [u[0] * cx + v[0] * cy + axis[0] * mid, u[1] * cx + v[1] * cy + axis[1] * mid, u[2] * cx + v[2] * cy + axis[2] * mid],
      axis,
      diameterMm: radius * 2,
      depthMm: tMax - tMin,
      coverageRad: coverage,
      concave,
      faceIndices: cluster.slice(0, 400)
    });
    if (results.length >= 100) break;
  }
  context.cylinders = results;
  return results;
}

const FULL_CYLINDER_COVERAGE_RAD = 4.7; // ≥270° = full hole/pin, else fillet

function solve3(m, b) {
  const a = [m[0].concat(b[0]), m[1].concat(b[1]), m[2].concat(b[2])];
  for (let col = 0; col < 3; col += 1) {
    let pivot = col;
    for (let row = col + 1; row < 3; row += 1) {
      if (Math.abs(a[row][col]) > Math.abs(a[pivot][col])) pivot = row;
    }
    if (Math.abs(a[pivot][col]) < 1e-12) return null;
    [a[col], a[pivot]] = [a[pivot], a[col]];
    for (let row = 0; row < 3; row += 1) {
      if (row === col) continue;
      const factor = a[row][col] / a[col][col];
      for (let k = col; k < 4; k += 1) a[row][k] -= factor * a[col][k];
    }
  }
  return [a[0][3] / a[0][0], a[1][3] / a[1][1], a[2][3] / a[2][2]];
}

// --- Wall detectors (port of compute_thin_wall / compute_unsupported_wall) --

// Material-side thin pairing shared by both wall detectors: anti-parallel
// close faces with MATERIAL between them (negative signed gap). Slots across
// air are engravings, not walls.
function collectThinPairs(context, thresholdMm) {
  const thin = new Set();
  const links = [];
  forEachClosePair(context, thresholdMm * 1.6, (i, j) => {
    const ni = context.tris[i].normal;
    const nj = context.tris[j].normal;
    if (dot(ni, nj) >= -0.7) return;
    const diff = subtract(context.tris[j].centroid, context.tris[i].centroid);
    const signedGap = dot(diff, ni);
    if (signedGap >= 0 || -signedGap >= thresholdMm) return;
    thin.add(i);
    thin.add(j);
    links.push([i, j]);
  });
  return { thin, links };
}

function detectThinWalls(context, thresholdMm, minRegionSpanMm) {
  const { thin, links } = collectThinPairs(context, thresholdMm);
  if (thin.size === 0) return { count: 0, centroids: [], faceIndices: [] };
  const linkAdj = new Map();
  for (const [a, b] of links) {
    if (!linkAdj.has(a)) linkAdj.set(a, []);
    if (!linkAdj.has(b)) linkAdj.set(b, []);
    linkAdj.get(a).push(b);
    linkAdj.get(b).push(a);
  }
  const clusters = [];
  const visited = new Set();
  for (const start of thin) {
    if (visited.has(start)) continue;
    const cluster = [];
    const queue = [start];
    while (queue.length > 0) {
      const cur = queue.pop();
      if (visited.has(cur)) continue;
      visited.add(cur);
      cluster.push(cur);
      for (const n of context.adjacency[cur]) {
        if (thin.has(n) && !visited.has(n)) queue.push(n);
      }
      for (const n of linkAdj.get(cur) || []) {
        if (!visited.has(n)) queue.push(n);
      }
    }
    if (cluster.length >= 4) clusters.push(cluster);
  }
  const centroids = [];
  const faceIndices = [];
  let count = 0;
  for (const cluster of clusters) {
    if (minRegionSpanMm != null) {
      const { min, max } = clusterBounds(context, cluster);
      const spans = [max[0] - min[0], max[1] - min[1], max[2] - min[2]].sort((a, b) => a - b);
      if (spans[1] < minRegionSpanMm) continue;
    }
    centroids.push(clusterCentroid(context, cluster));
    for (const f of cluster.slice(0, 2000)) faceIndices.push(f);
    count += 1;
  }
  return { count, centroids, faceIndices };
}

function detectUnsupportedWalls(context, thresholdMm) {
  const { thin, links } = collectThinPairs(context, thresholdMm);
  if (thin.size === 0) return { count: 0, centroids: [], faceIndices: [] };

  // Absorb rim caps: adjacent faces whose smallest bounding extent is on the
  // order of the wall thickness (the wall's own edge band).
  const wall = new Set(thin);
  for (let pass = 0; pass < 3; pass += 1) {
    let grew = false;
    for (const f of Array.from(wall)) {
      for (const g of context.adjacency[f]) {
        if (wall.has(g)) continue;
        const verts = context.faceVertexPositions(g);
        const ext = [0, 1, 2].map(axis => {
          const vals = verts.map(p => p[axis]);
          return Math.max(...vals) - Math.min(...vals);
        });
        if (Math.min(...ext) < thresholdMm * 1.3) {
          wall.add(g);
          grew = true;
        }
      }
    }
    if (!grew) break;
  }

  const linkAdj = new Map();
  for (const [a, b] of links) {
    if (!linkAdj.has(a)) linkAdj.set(a, []);
    if (!linkAdj.has(b)) linkAdj.set(b, []);
    linkAdj.get(a).push(b);
    linkAdj.get(b).push(a);
  }

  const centroids = [];
  const faceIndices = [];
  let count = 0;
  const visited = new Set();
  for (const start of wall) {
    if (visited.has(start)) continue;
    const comp = [];
    const queue = [start];
    while (queue.length > 0) {
      const cur = queue.pop();
      if (visited.has(cur)) continue;
      visited.add(cur);
      comp.push(cur);
      for (const n of context.adjacency[cur]) {
        if (wall.has(n) && !visited.has(n)) queue.push(n);
      }
      for (const n of linkAdj.get(cur) || []) {
        if (!visited.has(n)) queue.push(n);
      }
    }
    if (comp.length < 6) continue;

    const compSet = new Set(comp);
    const attached = [];
    let boundaryEdges = 0;
    for (const f of comp) {
      for (const g of context.adjacency[f]) {
        if (!compSet.has(g)) {
          boundaryEdges += 1;
          attached.push(context.tris[f].centroid);
        }
      }
    }

    const centroid = clusterCentroid(context, comp);
    let unsupported;
    if (boundaryEdges === 0) {
      unsupported = true; // free-standing shell (thin tube, lone plate)
    } else {
      // In-plane frame: e1 = longest wall direction, e2 = second, from the
      // cluster bounds; thickness axis is the smallest.
      const { min, max } = clusterBounds(context, comp);
      const spans = [max[0] - min[0], max[1] - min[1], max[2] - min[2]];
      const order = [0, 1, 2].sort((a, b) => spans[b] - spans[a]);
      const e1 = [0, 0, 0]; e1[order[0]] = 1;
      const e2 = [0, 0, 0]; e2[order[1]] = 1;
      const lo1 = min[order[0]]; const hi1 = max[order[0]];
      const lo2 = min[order[1]]; const hi2 = max[order[1]];
      const span1 = Math.max(hi1 - lo1, 1e-9);
      const span2 = Math.max(hi2 - lo2, 1e-9);
      const band = 0.18;
      const minEdges = Math.max(3, Math.floor(0.05 * boundaryEdges));

      // Closed cylindrical shells are periodic in e2: ring attachments span
      // the whole e2 range but are physically ONE side per axial end.
      let coverage = 0;
      {
        const angles = comp
          .map(f => {
            const c = context.tris[f].centroid;
            return Math.atan2(c[order[2]] - centroid[order[2]], c[order[1]] - centroid[order[1]]);
          })
          .sort((a, b) => a - b);
        if (angles.length >= 3) {
          let maxGap = 2 * Math.PI - (angles[angles.length - 1] - angles[0]);
          for (let n = 1; n < angles.length; n += 1) maxGap = Math.max(maxGap, angles[n] - angles[n - 1]);
          coverage = 2 * Math.PI - maxGap;
        }
      }
      const isClosedShell = coverage > 5.24; // > 300°

      let sides = 0;
      const s1 = attached.map(p => dot(p, e1));
      if (s1.filter(v => v <= lo1 + band * span1).length >= minEdges) sides += 1;
      if (s1.filter(v => v >= hi1 - band * span1).length >= minEdges) sides += 1;
      if (!isClosedShell) {
        const s2 = attached.map(p => dot(p, e2));
        if (s2.filter(v => v <= lo2 + band * span2).length >= minEdges) sides += 1;
        if (s2.filter(v => v >= hi2 - band * span2).length >= minEdges) sides += 1;
      }
      unsupported = sides < 2;
    }

    if (unsupported) {
      centroids.push(centroid);
      for (const f of comp.slice(0, 2000)) faceIndices.push(f);
      count += 1;
    }
  }
  return { count, centroids, faceIndices };
}

// --- Printing detectors ------------------------------------------------------

function detectBridgesLocal(context, maxSpanMm) {
  const partHeight = context.maxZ - context.minZ;
  const plateBand = Math.max(1.0, partHeight * 0.01);
  const cutoff = context.minZ + plateBand;
  const candidates = [];
  for (let f = 0; f < context.faceCount; f += 1) {
    const t = context.tris[f];
    if (t.normal[2] < -0.5 && t.centroid[2] > cutoff + 1e-6) candidates.push(f);
  }
  if (candidates.length === 0) return { count: 0, centroids: [], faceIndices: [] };

  const bridgeSet = new Set();
  for (const f of candidates.slice(0, 20000)) {
    const c = context.tris[f].centroid;
    const hits = verticalHits(context, c[0], c[1], f);
    let supportDistance = null;
    for (const hit of hits) {
      const d = c[2] - hit.z;
      // Touching geometry (d ≈ 0, e.g. a face resting on another body) IS
      // support, not a bridge.
      if (d >= -1e-3 && (supportDistance === null || d < supportDistance)) supportDistance = d;
    }
    if (supportDistance === null || supportDistance > maxSpanMm) bridgeSet.add(f);
  }
  if (bridgeSet.size === 0) return { count: 0, centroids: [], faceIndices: [] };

  const centroids = [];
  const faceIndices = [];
  let count = 0;
  for (const cluster of clusterFaceSet(context, bridgeSet)) {
    const { min, max } = clusterBounds(context, cluster);
    const horizontalSpan = Math.max(max[0] - min[0], max[1] - min[1]);
    if (horizontalSpan <= maxSpanMm) continue;
    centroids.push(clusterCentroid(context, cluster));
    for (const f of cluster.slice(0, 200)) faceIndices.push(f);
    count += 1;
  }
  return { count, centroids, faceIndices };
}

function detectSmallFeaturesLocal(context, minSizeMm) {
  // Per-face max dihedral angle guard: smooth-surface tessellation is not a feature.
  const maxDihedral = new Float32Array(context.faceCount);
  for (const faces of context.edgeFaces.values()) {
    if (faces.length !== 2) continue;
    const [i, j] = faces;
    const angle = Math.acos(Math.min(1, Math.max(-1, dot(context.tris[i].normal, context.tris[j].normal))));
    if (angle > maxDihedral[i]) maxDihedral[i] = angle;
    if (angle > maxDihedral[j]) maxDihedral[j] = angle;
  }
  const SMOOTH_THRESHOLD = 0.09; // ≈5°

  const smallSet = new Set();
  for (let f = 0; f < context.faceCount; f += 1) {
    const [a, b, c] = context.faceVertexPositions(f);
    const maxEdge = Math.max(
      vectorLength(subtract(b, a)),
      vectorLength(subtract(c, b)),
      vectorLength(subtract(a, c))
    );
    if (maxEdge >= minSizeMm) continue;
    if (maxDihedral[f] < SMOOTH_THRESHOLD) continue;
    smallSet.add(f);
  }
  if (smallSet.size === 0) return { count: 0, centroids: [], faceIndices: [] };

  const centroids = [];
  const faceIndices = [];
  let count = 0;
  for (const cluster of clusterFaceSet(context, smallSet)) {
    if (cluster.length > 50) continue; // long curved strips are not features
    const { min, max } = clusterBounds(context, cluster);
    const diag = Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]);
    if (diag >= minSizeMm * 2) continue;
    let clusterArea = 0;
    for (const f of cluster) clusterArea += context.tris[f].areaMm2;
    const minArea = Math.PI * (minSizeMm / 2) ** 2 * 0.25;
    if (clusterArea < minArea) continue;
    centroids.push(clusterCentroid(context, cluster));
    for (const f of cluster.slice(0, 500)) faceIndices.push(f);
    count += 1;
  }
  return { count, centroids, faceIndices };
}

function detectEmbossEngraveLocal(context, minHeightMm) {
  if (context.faceCount < 20) return { count: 0, centroids: [], faceIndices: [] };
  const upFaces = [];
  for (let f = 0; f < context.faceCount; f += 1) {
    if (context.tris[f].normal[2] > 0.7) upFaces.push(f);
  }
  if (upFaces.length < 20) return { count: 0, centroids: [], faceIndices: [] };

  // Area-weighted dominant top plane.
  const zTolerance = Math.max(minHeightMm * 0.05, 0.05);
  const zBins = new Map();
  for (const f of upFaces) {
    const z = context.tris[f].centroid[2];
    const area = context.tris[f].areaMm2;
    const key = Math.round(z / zTolerance);
    const bin = zBins.get(key) || [0, 0];
    bin[0] += z * area;
    bin[1] += area;
    zBins.set(key, bin);
  }
  let refZ = null; let bestArea = 0;
  for (const [wz, area] of zBins.values()) {
    if (area > bestArea) { bestArea = area; refZ = wz / area; }
  }
  if (refZ === null || bestArea <= 0) return { count: 0, centroids: [], faceIndices: [] };

  const maxDihedral = new Float32Array(context.faceCount);
  for (const faces of context.edgeFaces.values()) {
    if (faces.length !== 2) continue;
    const [i, j] = faces;
    const angle = Math.acos(Math.min(1, Math.max(-1, dot(context.tris[i].normal, context.tris[j].normal))));
    if (angle > maxDihedral[i]) maxDihedral[i] = angle;
    if (angle > maxDihedral[j]) maxDihedral[j] = angle;
  }

  const minDetailHeight = minHeightMm * 0.15;
  const flagged = new Set();
  for (const f of upFaces) {
    const delta = Math.abs(context.tris[f].centroid[2] - refZ);
    if (delta < minDetailHeight || delta >= minHeightMm) continue;
    if (maxDihedral[f] < 0.09) continue;
    flagged.add(f);
  }
  if (flagged.size === 0) return { count: 0, centroids: [], faceIndices: [] };

  const centroids = [];
  const faceIndices = [];
  let count = 0;
  for (const cluster of clusterFaceSet(context, flagged)) {
    const { min, max } = clusterBounds(context, cluster);
    const diag = Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]);
    if (diag > minHeightMm * 10) continue; // structural steps, not details
    centroids.push(clusterCentroid(context, cluster));
    for (const f of cluster.slice(0, 500)) faceIndices.push(f);
    count += 1;
  }
  return { count, centroids, faceIndices };
}

// Enclosed voids: an inner watertight shell strictly inside another body's
// bounds. Powder/resin cannot escape from these without an escape hole.
function detectEnclosedVoids(context) {
  if (!context.metrics.isManifold || context.bodies.length <= 1) return [];
  const enclosed = [];
  for (const inner of context.bodies) {
    for (const outer of context.bodies) {
      if (inner === outer) continue;
      const inside = [0, 1, 2].every(axis =>
        inner.min[axis] > outer.min[axis] + 1e-9 && inner.max[axis] < outer.max[axis] - 1e-9);
      if (inside) {
        enclosed.push(inner);
        break;
      }
    }
  }
  return enclosed;
}

function detectClearanceLocal(context, clearanceMm) {
  // Minimum inter-body distance below the moving/connecting clearance.
  const bodies = context.bodies;
  if (bodies.length <= 1) return { count: 0, centroids: [], faceIndices: [] };
  const centroids = [];
  const faceIndices = [];
  let count = 0;
  for (let a = 0; a < bodies.length; a += 1) {
    for (let b = a + 1; b < bodies.length; b += 1) {
      // Quick reject: bbox gap already above clearance.
      const gap = [0, 1, 2].map(axis =>
        Math.max(0, Math.max(bodies[a].min[axis] - bodies[b].max[axis], bodies[b].min[axis] - bodies[a].max[axis])));
      if (Math.hypot(gap[0], gap[1], gap[2]) >= clearanceMm) continue;
      let minDistSq = Infinity;
      let nearFaceA = -1;
      const sampleA = bodies[a].faces.filter((_, i) => i % Math.max(1, Math.floor(bodies[a].faces.length / 800)) === 0);
      const sampleB = bodies[b].faces.filter((_, i) => i % Math.max(1, Math.floor(bodies[b].faces.length / 800)) === 0);
      for (const fa of sampleA) {
        const ca = context.tris[fa].centroid;
        for (const fb of sampleB) {
          const cb = context.tris[fb].centroid;
          const dx = ca[0] - cb[0]; const dy = ca[1] - cb[1]; const dz = ca[2] - cb[2];
          const dSq = dx * dx + dy * dy + dz * dz;
          if (dSq < minDistSq) { minDistSq = dSq; nearFaceA = fa; }
        }
      }
      if (minDistSq < clearanceMm * clearanceMm && nearFaceA >= 0) {
        centroids.push(context.tris[nearFaceA].centroid.slice());
        faceIndices.push(nearFaceA);
        count += 1;
      }
    }
  }
  return { count, centroids, faceIndices };
}

// --- CNC milling detectors ---------------------------------------------------

function collectConcaveEdges(context) {
  const concave = [];
  for (const [key, faces] of context.edgeFaces.entries()) {
    if (faces.length !== 2) continue;
    const [i, j] = faces;
    const diff = subtract(context.tris[j].centroid, context.tris[i].centroid);
    if (dot(context.tris[i].normal, diff) < -0.05 && dot(context.tris[j].normal, [-diff[0], -diff[1], -diff[2]]) < -0.05) {
      concave.push({ key, faces: [i, j] });
    }
  }
  return concave;
}

function detectSharpCornersLocal(context, thresholdDeg) {
  const cosThresh = Math.cos((thresholdDeg * Math.PI) / 180);
  const mids = [];
  const midFaces = [];
  for (const { faces } of collectConcaveEdges(context)) {
    const [i, j] = faces;
    const dn = dot(context.tris[i].normal, context.tris[j].normal);
    if (dn > cosThresh) {
      const mid = context.tris[i].centroid.map((v, axis) => (v + context.tris[j].centroid[axis]) / 2);
      mids.push(mid);
      midFaces.push([i, j]);
    }
  }
  if (mids.length === 0) return { count: 0, centroids: [], faceIndices: [] };
  const extent = Math.max(context.metrics.boundingBox.x, context.metrics.boundingBox.y, context.metrics.boundingBox.z);
  const clusterRadius = Math.max(3.0, extent * 0.03);
  const used = new Set();
  const centroids = [];
  const faceIndices = [];
  let count = 0;
  for (let i = 0; i < mids.length; i += 1) {
    if (used.has(i)) continue;
    for (let j = 0; j < mids.length; j += 1) {
      const d = Math.hypot(mids[j][0] - mids[i][0], mids[j][1] - mids[i][1], mids[j][2] - mids[i][2]);
      if (d < clusterRadius) used.add(j);
    }
    centroids.push(mids[i]);
    faceIndices.push(midFaces[i][0], midFaces[i][1]);
    count += 1;
  }
  return { count, centroids, faceIndices };
}

function detectInternalRadiiLocal(context) {
  // Accurate fillet radii from partial concave cylinders + sharp concave
  // corners (radius ≈ 0) from edge clustering.
  const results = [];
  for (const cyl of detectCylinders(context)) {
    if (cyl.concave && cyl.coverageRad < FULL_CYLINDER_COVERAGE_RAD) {
      results.push({ radiusMm: cyl.diameterMm / 2, centroid: cyl.center, faceIndices: cyl.faceIndices.slice(0, 50) });
    }
  }
  const sharp = detectSharpCornersLocal(context, 89);
  for (let i = 0; i < sharp.count && i < 50; i += 1) {
    results.push({ radiusMm: 0.0, centroid: sharp.centroids[i], faceIndices: sharp.faceIndices.slice(i * 2, i * 2 + 2) });
  }
  return results.slice(0, 100);
}

function detectCavitiesLocal(context) {
  const floorSet = new Set();
  for (let f = 0; f < context.faceCount; f += 1) {
    if (context.tris[f].normal[2] < -0.7) floorSet.add(f);
  }
  if (floorSet.size === 0) return [];
  const cavities = [];
  for (const cluster of clusterFaceSet(context, floorSet)) {
    const { min, max } = clusterBounds(context, cluster);
    const width = Math.max(max[0] - min[0], max[1] - min[1]);
    if (width < 0.5) continue;
    const centroid = clusterCentroid(context, cluster);
    const hits = verticalHits(context, centroid[0], centroid[1]);
    let topZ = null;
    for (const hit of hits) {
      if (hit.z > centroid[2] + 1e-3 && (topZ === null || hit.z < topZ)) topZ = hit.z;
    }
    if (topZ === null) continue;
    const depth = topZ - centroid[2];
    if (depth < 1.0) continue;
    cavities.push({
      widthMm: Math.max(width, 1.0),
      depthMm: depth,
      depthRatio: depth / Math.max(width, 1.0),
      centroid,
      faceIndices: cluster.slice(0, 50)
    });
  }
  return cavities.slice(0, 50);
}

function detectChatterLocal(context) {
  const minAreaMm2 = MILLING_RULES.chatterMinAreaCm2 * 100;
  const centroids = [];
  const faceIndices = [];
  let count = 0;
  for (let f = 0; f < context.faceCount; f += 1) {
    const t = context.tris[f];
    if (Math.abs(t.normal[2]) <= 0.8 || t.areaMm2 <= minAreaMm2) continue;
    const hits = verticalHits(context, t.centroid[0], t.centroid[1], f);
    let below = null;
    for (const hit of hits) {
      if (hit.z < t.centroid[2] - 0.1 && (below === null || hit.z > below)) below = hit.z;
    }
    if (below === null) continue;
    const thickness = t.centroid[2] - below;
    if (thickness <= MILLING_RULES.chatterMaxThicknessMm) {
      centroids.push(t.centroid.slice());
      faceIndices.push(f);
      count += 1;
    }
  }
  return { count, centroids: centroids.slice(0, 50), faceIndices: faceIndices.slice(0, 50) };
}

function detectToolAccessLocal(context) {
  const dirs = [[0, 0, 1], [0, 0, -1], [1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]];
  const inaccessible3 = [];
  for (let f = 0; f < context.faceCount; f += 1) {
    const n = context.tris[f].normal;
    let reachable = false;
    for (const d of dirs) {
      if (dot(n, d) > 0.1) { reachable = true; break; }
    }
    if (!reachable) inaccessible3.push(f);
  }
  if (inaccessible3.length === 0) {
    return { minimumAxes: 3, inaccessible: [], details: "All faces reachable in 3-axis setup" };
  }
  const needFive = inaccessible3.filter(f => {
    const n = context.tris[f].normal;
    return Math.hypot(n[0], n[1]) <= Math.abs(n[2]);
  });
  if (needFive.length === 0) {
    return { minimumAxes: 4, inaccessible: inaccessible3.slice(0, 100), details: `${inaccessible3.length} face(s) require 4-axis rotation` };
  }
  return { minimumAxes: 5, inaccessible: needFive.slice(0, 100), details: `${needFive.length} face(s) require 5-axis machining` };
}

function detectUndercutsLocal(context) {
  const partHeight = context.maxZ - context.minZ;
  const plateBand = Math.max(1.0, partHeight * 0.01);
  const candidates = [];
  for (let f = 0; f < context.faceCount; f += 1) {
    const t = context.tris[f];
    if (t.normal[2] < -0.15 && t.centroid[2] > context.minZ + plateBand) candidates.push(f);
  }
  const undercutSet = new Set();
  for (const f of candidates.slice(0, 20000)) {
    const c = context.tris[f].centroid;
    const hits = verticalHits(context, c[0], c[1], f);
    for (const hit of hits) {
      if (hit.z > c[2] + 1e-3) { undercutSet.add(f); break; } // shadowed from above
    }
  }
  if (undercutSet.size === 0) return { count: 0, centroids: [], faceIndices: [] };
  const centroids = [];
  const faceIndices = [];
  let count = 0;
  for (const cluster of clusterFaceSet(context, undercutSet)) {
    if (cluster.length < 2) continue;
    centroids.push(clusterCentroid(context, cluster));
    for (const f of cluster.slice(0, 200)) faceIndices.push(f);
    count += 1;
  }
  return { count, centroids, faceIndices };
}

// --- CNC turning detectors ---------------------------------------------------

function radialProfile(context, axisIndex, stations) {
  const { positions } = context.mesh;
  const perp = [0, 1, 2].filter(a => a !== axisIndex);
  let lo = Infinity; let hi = -Infinity;
  for (let i = 0; i < positions.length; i += 3) {
    const v = positions[i + axisIndex];
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!(hi - lo > 1e-6)) return null;
  const buckets = Array.from({ length: stations }, () => []);
  const bucketFor = value => Math.min(
    stations - 1,
    Math.max(0, Math.floor(((value - lo) / (hi - lo)) * stations))
  );
  for (let i = 0; i < positions.length; i += 3) {
    buckets[bucketFor(positions[i + axisIndex])]
      .push([positions[i + perp[0]], positions[i + perp[1]]]);
  }
  // Prismatic tessellations put vertices only at the ends of long faces, so
  // interior stations would be empty. Sample every mesh edge where it crosses
  // each station plane (interpolated), which fills the full profile.
  const stationValue = s => lo + ((s + 0.5) / stations) * (hi - lo);
  {
    const seenEdges = new Set();
    const { indices } = context.mesh;
    for (let t = 0; t < indices.length; t += 3) {
      for (let k = 0; k < 3; k += 1) {
        const v0 = indices[t + k];
        const v1 = indices[t + ((k + 1) % 3)];
        const key = v0 < v1 ? `${v0}:${v1}` : `${v1}:${v0}`;
        if (seenEdges.has(key)) continue;
        seenEdges.add(key);
        const a0 = positions[v0 * 3 + axisIndex];
        const a1 = positions[v1 * 3 + axisIndex];
        const eLo = Math.min(a0, a1);
        const eHi = Math.max(a0, a1);
        if (eHi - eLo < 1e-9) continue;
        const sStart = Math.max(0, Math.ceil(((eLo - lo) / (hi - lo)) * stations - 0.5));
        const sEnd = Math.min(stations - 1, Math.floor(((eHi - lo) / (hi - lo)) * stations - 0.5));
        for (let s = sStart; s <= sEnd; s += 1) {
          const sv = stationValue(s);
          if (sv < eLo || sv > eHi) continue;
          const t01 = (sv - a0) / (a1 - a0);
          if (!Number.isFinite(t01)) continue;
          const p0a = positions[v0 * 3 + perp[0]];
          const p0b = positions[v0 * 3 + perp[1]];
          const p1a = positions[v1 * 3 + perp[0]];
          const p1b = positions[v1 * 3 + perp[1]];
          buckets[s].push([p0a + t01 * (p1a - p0a), p0b + t01 * (p1b - p0b)]);
        }
      }
    }
  }
  const profile = [];
  for (let s = 0; s < stations; s += 1) {
    const pts = buckets[s];
    if (pts.length < 6) continue;
    let cx = 0; let cy = 0;
    for (const [x, y] of pts) { cx += x; cy += y; }
    cx /= pts.length; cy /= pts.length;
    let sum = 0; let sumSq = 0; let maxR = 0;
    for (const [x, y] of pts) {
      const r = Math.hypot(x - cx, y - cy);
      sum += r; sumSq += r * r;
      if (r > maxR) maxR = r;
    }
    const mean = sum / pts.length;
    const std = Math.sqrt(Math.max(0, sumSq / pts.length - mean * mean));
    profile.push({ station: lo + ((s + 0.5) / stations) * (hi - lo), meanR: mean, stdR: std, maxR });
  }
  return { profile, length: hi - lo };
}

function detectAxialSymmetryLocal(context) {
  let best = null;
  for (let axisIndex = 0; axisIndex < 3; axisIndex += 1) {
    const rp = radialProfile(context, axisIndex, 24);
    if (!rp || rp.profile.length < 5 || rp.length < 1.0) continue;
    const deviations = rp.profile.filter(p => p.meanR > 0).map(p => p.stdR / p.meanR);
    if (deviations.length === 0) continue;
    const meanDeviation = deviations.reduce((a, b) => a + b, 0) / deviations.length;
    const meanDiameter = (rp.profile.reduce((a, p) => a + p.meanR, 0) / rp.profile.length) * 2;
    const candidate = {
      axisIndex,
      deviation: meanDeviation,
      isTurnable: meanDeviation < TURNING_RULES.symmetryThreshold,
      ldRatio: meanDiameter > 0 ? rp.length / meanDiameter : null
    };
    if (best === null || candidate.deviation < best.deviation) best = candidate;
  }
  return best;
}

function detectGroovesLocal(context, axisIndex) {
  const rp = radialProfile(context, axisIndex, 100);
  if (!rp || rp.profile.length < 5) return [];
  const outer = rp.profile.map(p => p.maxR);
  const meanR = outer.reduce((a, b) => a + b, 0) / outer.length;
  const depthThresh = meanR * 0.05;
  const grooves = [];
  let inGroove = false;
  let start = 0;
  let minR = meanR;
  for (let i = 0; i < rp.profile.length; i += 1) {
    const r = outer[i];
    const station = rp.profile[i].station;
    if (!inGroove && r < meanR - depthThresh) {
      inGroove = true;
      start = station;
      minR = r;
    } else if (inGroove) {
      if (r < minR) minR = r;
      if (r >= meanR - depthThresh * 0.5 || i === rp.profile.length - 1) {
        grooves.push({ widthMm: station - start, depthMm: meanR - minR, station: (start + station) / 2 });
        inGroove = false;
      }
    }
  }
  return grooves.slice(0, 50);
}

// --- Advisory heuristics (new checks; documented as advisory severity) -------

function detectWarpageLocal(context) {
  const bb = context.metrics.boundingBox;
  const extents = [bb.x, bb.y, bb.z];
  const maxExtent = Math.max(...extents);
  const minExtent = Math.min(...extents);
  if (!(minExtent > 0)) return null;
  const aspect = maxExtent / minExtent;
  if (maxExtent < WARPAGE_MIN_SPAN_MM || minExtent > WARPAGE_MAX_THICKNESS_MM || aspect < WARPAGE_ASPECT_THRESHOLD) {
    return null;
  }
  const thinAxis = extents.indexOf(minExtent);
  const faceIndices = [];
  for (let f = 0; f < context.faceCount && faceIndices.length < 2000; f += 1) {
    if (Math.abs(context.tris[f].normal[thinAxis]) > 0.8) faceIndices.push(f);
  }
  return { aspect, faceIndices };
}

function detectSurfaceDefectLocal(context) {
  // Shallow-slope up-facing faces show visible layer stepping (staircase).
  const lo = Math.cos((25 * Math.PI) / 180);
  const hi = Math.cos((2 * Math.PI) / 180);
  const faces = [];
  let area = 0;
  for (let f = 0; f < context.faceCount; f += 1) {
    const nz = context.tris[f].normal[2];
    if (nz > lo && nz < hi) {
      faces.push(f);
      area += context.tris[f].areaMm2;
    }
  }
  if (area < STAIRCASE_MIN_AREA_MM2) return null;
  return { areaMm2: area, faceIndices: faces.slice(0, 2000) };
}

function analyzeSheetMetalLocal(context, issues) {
  // Gauge detection: material thickness sampled per face, area-weighted.
  // Vertical faces get the opposing-pair method; up/down faces measure the
  // vertical material span below/above them (robust on coarse tessellations
  // where large flat faces have few, misaligned triangle centroids).
  const samples = []; // { t, areaMm2, face }
  for (let f = 0; f < context.faceCount; f += 1) {
    const t = context.tris[f];
    if (Math.abs(t.normal[2]) < 0.7) continue;
    const hits = verticalHits(context, t.centroid[0], t.centroid[1], f);
    let thickness = null;
    if (t.normal[2] > 0) {
      for (const hit of hits) {
        const d = t.centroid[2] - hit.z;
        if (d > 1e-3 && (thickness === null || d < thickness)) thickness = d;
      }
    } else {
      for (const hit of hits) {
        const d = hit.z - t.centroid[2];
        if (d > 1e-3 && (thickness === null || d < thickness)) thickness = d;
      }
    }
    if (thickness !== null) samples.push({ t: thickness, areaMm2: t.areaMm2, face: f });
  }
  forEachClosePair(context, SHEET_METAL_RULES.maxThicknessMm * 1.6, (i, j) => {
    const ni = context.tris[i].normal;
    if (Math.abs(ni[2]) >= 0.7) return; // vertical-ish faces only here
    if (dot(ni, context.tris[j].normal) >= -0.7) return;
    const signedGap = dot(subtract(context.tris[j].centroid, context.tris[i].centroid), ni);
    if (signedGap >= 0) return;
    samples.push({ t: -signedGap, areaMm2: context.tris[i].areaMm2, face: i });
  });

  const valid = samples.filter(s =>
    s.t >= SHEET_METAL_RULES.minThicknessMm && s.t <= SHEET_METAL_RULES.maxThicknessMm);
  const totalArea = context.tris.reduce((sum, t) => sum + t.areaMm2, 0);
  if (valid.length === 0 || totalArea <= 0) {
    issues.push(issue(
      "sheet_uniformity", "warning", "Not a uniform sheet part",
      "No dominant sheet gauge between "
        + `${SHEET_METAL_RULES.minThicknessMm} and ${SHEET_METAL_RULES.maxThicknessMm} mm was found; `
        + "sheet metal fabrication expects a constant-gauge part.",
      0, SHEET_METAL_RULES.uniformityCoverage));
    return;
  }

  const bins = new Map();
  for (const s of valid) {
    const key = Math.round(s.t * 10); // 0.1 mm bins
    bins.set(key, (bins.get(key) || 0) + s.areaMm2);
  }
  let gaugeKey = null; let gaugeArea = 0;
  for (const [key, area] of bins.entries()) {
    if (area > gaugeArea) { gaugeArea = area; gaugeKey = key; }
  }
  const gauge = gaugeKey / 10;
  let matchedArea = 0;
  for (const s of valid) {
    if (Math.abs(s.t - gauge) <= 0.15) matchedArea += s.areaMm2;
  }
  // Each sampled face implies a matching opposite face of similar area.
  const coverage = Math.min(1, (matchedArea * 2) / totalArea);
  if (coverage < SHEET_METAL_RULES.uniformityCoverage) {
    issues.push(issue(
      "sheet_uniformity", "warning", "Thickness is not uniform",
      `Only ${(coverage * 100).toFixed(0)}% of the surface sits at the dominant ${gauge.toFixed(1)} mm gauge. Sheet metal parts must keep one constant thickness.`,
      coverage, SHEET_METAL_RULES.uniformityCoverage));
  }

  const cylinders = detectCylinders(context);
  const minBendRadius = gauge * SHEET_METAL_RULES.minBendRadiusFactor;
  const tightBends = cylinders.filter(c =>
    !c.concave && c.coverageRad < FULL_CYLINDER_COVERAGE_RAD && c.diameterMm / 2 < minBendRadius);
  if (tightBends.length > 0) {
    const minR = Math.min(...tightBends.map(c => c.diameterMm / 2));
    issues.push(issue(
      "bend_radius", "warning", `Tight Bends (${tightBends.length})`,
      `${tightBends.length} bend(s) with radius below 1× material thickness (${gauge.toFixed(1)} mm). Tight bends crack or spring back unpredictably. Smallest: R${minR.toFixed(2)} mm.`,
      minR, minBendRadius,
      tightBends.flatMap(c => c.faceIndices).slice(0, 2000), tightBends[0].center));
  }

  const minHole = gauge * SHEET_METAL_RULES.minHoleDiameterFactor;
  const smallHoles = cylinders.filter(c =>
    c.concave && c.coverageRad >= FULL_CYLINDER_COVERAGE_RAD && c.diameterMm < minHole);
  if (smallHoles.length > 0) {
    const minD = Math.min(...smallHoles.map(c => c.diameterMm));
    issues.push(issue(
      "hole", "warning", `Small Punched Holes (${smallHoles.length})`,
      `${smallHoles.length} hole(s) below 1× material thickness (${gauge.toFixed(1)} mm) — punches deform or break. Smallest: Ø${minD.toFixed(2)} mm.`,
      minD, minHole,
      smallHoles.flatMap(c => c.faceIndices).slice(0, 2000), smallHoles[0].center));
  }
}

function analyzeSiliconeCastingLocal(context, issues) {
  const voids = detectEnclosedVoids(context);
  if (voids.length > 0) {
    issues.push(issue(
      "trapped_volume", "error", `Trapped Air Pockets (${voids.length})`,
      `${voids.length} fully enclosed cavity/cavities will trap air and uncured silicone during casting. Add vents or split the part.`,
      voids.length, 0,
      voids.flatMap(b => b.faces.slice(0, 200)).slice(0, 2000), voids[0].centroid));
  }
  const thin = detectThinWalls(context, SILICONE_RULES.minWallMm, null);
  if (thin.count > 0) {
    issues.push(issue(
      "thin_wall", "warning", `Thin Walls (${thin.count} regions)`,
      `${thin.count} wall region(s) below the ${SILICONE_RULES.minWallMm} mm silicone casting minimum.`,
      thin.count, SILICONE_RULES.minWallMm, thin.faceIndices, thin.centroids[0] || []));
  }
  const tearSlots = detectCavitiesLocal(context).filter(c =>
    c.widthMm < SILICONE_RULES.tearSlotMaxWidthMm && c.depthRatio > SILICONE_RULES.tearSlotDepthRatio);
  if (tearSlots.length > 0) {
    const worst = Math.max(...tearSlots.map(c => c.depthRatio));
    issues.push(issue(
      "tear_risk", "warning", `Mold Tear Risk (${tearSlots.length})`,
      `${tearSlots.length} deep narrow slot(s) (worst ${worst.toFixed(1)}:1 depth/width) will tear the silicone mold on demolding.`,
      worst, SILICONE_RULES.tearSlotDepthRatio,
      tearSlots.flatMap(c => c.faceIndices).slice(0, 2000), tearSlots[0].centroid));
  }
}

// --- Process family analyzers -------------------------------------------------

function analyzePrintingProcess(context, processCode, issues) {
  const rules = PRINTING_RULES[processCode];
  if (!rules) return;

  const thin = detectThinWalls(context, rules.supportedWallMm, rules.minFeatureMm);
  if (thin.count > 0) {
    issues.push(issue(
      "thin_wall", "warning", `Thin Walls (${thin.count} regions)`,
      `${thin.count} wall region(s) below the ${processCode} minimum of ${rules.supportedWallMm} mm.`,
      thin.count, rules.supportedWallMm, thin.faceIndices, thin.centroids[0] || []));
  }

  if (rules.unsupportedWallMm != null) {
    const uw = detectUnsupportedWalls(context, rules.unsupportedWallMm);
    if (uw.count > 0) {
      issues.push(issue(
        "unsupported_wall", "warning", `Unsupported Walls (${uw.count} regions)`,
        `${uw.count} unsupported wall(s) below ${rules.unsupportedWallMm} mm.`,
        uw.count, rules.unsupportedWallMm, uw.faceIndices, uw.centroids[0] || []));
    }
  }

  if (!POWDER_BED_PROCESSES.has(processCode)) {
    const overhangDeg = rules.maxOverhangDeg ?? 45;
    const overhangLimit = -Math.sin(((overhangDeg + 0.5) * Math.PI) / 180);
    const partHeight = context.maxZ - context.minZ;
    const plateBand = Math.max(Math.max(1.0, partHeight * 0.01), rules.firstLayerThicknessMm);
    // Solid/touching probe (port of the Python solid-probe filter): a face
    // with geometry immediately below it is supported, not an overhang.
    const supportBand = Math.max(rules.firstLayerThicknessMm, 0.5);
    const overhangFaces = context.tris.filter(t => {
      if (t.centroid[2] <= context.minZ + plateBand || t.normal[2] >= overhangLimit) return false;
      const hits = verticalHits(context, t.centroid[0], t.centroid[1], t.faceIndex);
      for (const hit of hits) {
        const d = t.centroid[2] - hit.z;
        if (d >= -1e-3 && d <= supportBand) return false; // resting on material
      }
      return true;
    });
    if (overhangFaces.length > 0) {
      const overhangAreaMm2 = overhangFaces.reduce((sum, t) => sum + t.areaMm2, 0);
      const regionCount = groupOverhangRegions(overhangFaces, context.metrics.weldedIndices);
      issues.push(issue(
        "overhang", "warning", "Support Required",
        `Found ${regionCount.toLocaleString()} overhang region(s) (≈${Math.round(overhangAreaMm2).toLocaleString()} mm² total) steeper than ${overhangDeg}° that require supports.`,
        regionCount, 0,
        overhangFaces.map(t => t.faceIndex).slice(0, 4000), averageCentroid(overhangFaces)));
    }

    if (rules.bridgeSpanMm != null) {
      const bridges = detectBridgesLocal(context, rules.bridgeSpanMm);
      if (bridges.count > 0) {
        issues.push(issue(
          "bridge", "warning", `Long Bridges (${bridges.count})`,
          `${bridges.count} unsupported horizontal span(s) exceed the ${rules.bridgeSpanMm} mm bridge limit for ${processCode}.`,
          bridges.count, rules.bridgeSpanMm, bridges.faceIndices, bridges.centroids[0] || []));
      }
    }
  }

  const cylinders = detectCylinders(context);
  const holes = cylinders.filter(c => c.concave && c.coverageRad >= FULL_CYLINDER_COVERAGE_RAD);
  const smallHoles = holes.filter(c => c.diameterMm < rules.minHoleDiameterMm);
  if (smallHoles.length > 0) {
    const minD = Math.min(...smallHoles.map(c => c.diameterMm));
    issues.push(issue(
      "hole", "warning", `Small Holes (${smallHoles.length})`,
      `${smallHoles.length} hole(s) below the ${processCode} minimum reliable diameter of ${rules.minHoleDiameterMm} mm. Smallest: Ø${minD.toFixed(2)} mm.`,
      minD, rules.minHoleDiameterMm,
      smallHoles.flatMap(c => c.faceIndices).slice(0, 2000), smallHoles[0].center));
  }

  const pins = cylinders.filter(c =>
    !c.concave && c.coverageRad >= FULL_CYLINDER_COVERAGE_RAD
    && c.diameterMm < rules.pinDiameterMm && c.depthMm >= 0.6 * c.diameterMm);
  if (pins.length > 0) {
    const minD = Math.min(...pins.map(c => c.diameterMm));
    issues.push(issue(
      "pin", "warning", `Thin Pins (${pins.length})`,
      `${pins.length} pin/column feature(s) below the minimum diameter ${rules.pinDiameterMm} mm for ${processCode}. Smallest: Ø${minD.toFixed(2)} mm.`,
      minD, rules.pinDiameterMm,
      pins.flatMap(c => c.faceIndices).slice(0, 2000), pins[0].center));
  }

  const smallFeatures = detectSmallFeaturesLocal(context, rules.minFeatureMm);
  if (smallFeatures.count > 0) {
    issues.push(issue(
      "small_feature", "warning", `Small Features (${smallFeatures.count})`,
      `${smallFeatures.count} feature(s) smaller than the ${processCode} minimum of ${rules.minFeatureMm} mm.`,
      smallFeatures.count, rules.minFeatureMm, smallFeatures.faceIndices, smallFeatures.centroids[0] || []));
  }

  const emboss = detectEmbossEngraveLocal(context, rules.embossedHeightMm);
  if (emboss.count > 0) {
    issues.push(issue(
      "small_feature", "info", `Fine Embossed/Engraved Details (${emboss.count})`,
      `${emboss.count} embossed or engraved detail(s) shallower than the ${processCode} minimum of ${rules.embossedHeightMm} mm may not reproduce.`,
      emboss.count, rules.embossedHeightMm, emboss.faceIndices, emboss.centroids[0] || []));
  }

  const voids = detectEnclosedVoids(context);
  if (voids.length > 0 && RESIN_PROCESSES.has(processCode)) {
    issues.push(issue(
      "trapped_volume", "warning", `Resin Trapping Risk (${voids.length})`,
      `${voids.length} enclosed cavity/cavities will trap uncured resin. Add drainage holes or vent the volume.`,
      voids.length, 0,
      voids.flatMap(b => b.faces.slice(0, 200)).slice(0, 2000), voids[0].centroid));
  }
  if (voids.length > 0 && rules.escapeHoleDiameterMm != null) {
    const escapeHoles = holes.filter(c => c.diameterMm >= rules.escapeHoleDiameterMm);
    const blocked = voids.filter(inner => {
      return !escapeHoles.some(h => {
        const dz = Math.abs(h.center[2] - inner.centroid[2]);
        const lateral = Math.hypot(h.center[0] - inner.centroid[0], h.center[1] - inner.centroid[1]);
        const extent = Math.max(inner.max[0] - inner.min[0], inner.max[1] - inner.min[1], inner.max[2] - inner.min[2]);
        return dz < h.depthMm * 0.5 + extent && lateral < extent;
      });
    });
    if (blocked.length > 0) {
      issues.push(issue(
        "escape_hole", "warning", `Missing Escape Holes (${blocked.length})`,
        `${blocked.length} enclosed volume(s) lack an escape hole of at least Ø${rules.escapeHoleDiameterMm} mm for ${POWDER_BED_PROCESSES.has(processCode) ? "powder" : "resin"} removal.`,
        blocked.length, rules.escapeHoleDiameterMm,
        blocked.flatMap(b => b.faces.slice(0, 200)).slice(0, 2000), blocked[0].centroid));
    }
  }

  if (rules.connectingClearanceMm != null && context.bodies.length > 1) {
    const clearance = detectClearanceLocal(context, rules.connectingClearanceMm);
    if (clearance.count > 0) {
      issues.push(issue(
        "clearance", "warning", `Tight Clearances (${clearance.count})`,
        `${clearance.count} body pair(s) closer than the ${rules.connectingClearanceMm} mm ${processCode} clearance for separate/moving parts — they may fuse together.`,
        clearance.count, rules.connectingClearanceMm, clearance.faceIndices, clearance.centroids[0] || []));
    }
  }

  if (processCode === "FDM" || processCode === "SLS" || processCode === "MJF") {
    const warpage = detectWarpageLocal(context);
    if (warpage) {
      issues.push(issue(
        "warpage", "warning", "Warpage Risk",
        `Large flat geometry (aspect ratio ${warpage.aspect.toFixed(0)}:1) is prone to thermal warping on ${processCode}. Consider ribs, thicker sections, or a different process.`,
        warpage.aspect, WARPAGE_ASPECT_THRESHOLD, warpage.faceIndices,
        [0, 0, 0]));
    }
  }

  if (processCode === "FDM" || RESIN_PROCESSES.has(processCode)) {
    const staircase = detectSurfaceDefectLocal(context);
    if (staircase) {
      issues.push(issue(
        "surface_defect", "info", "Visible Layer Stepping",
        `≈${Math.round(staircase.areaMm2).toLocaleString()} mm² of shallow-sloped surface will show visible staircase layer lines. Reorient the part or use a finer layer height for cosmetic faces.`,
        staircase.areaMm2, STAIRCASE_MIN_AREA_MM2, staircase.faceIndices,
        [0, 0, 0]));
    }
  }
}

function analyzeCncMilling(context, issues) {
  const radii = detectInternalRadiiLocal(context);
  const smallRadii = radii.filter(r => r.radiusMm < MILLING_RULES.minInternalRadiusMm);
  if (smallRadii.length > 0) {
    const minR = Math.min(...smallRadii.map(r => r.radiusMm));
    const tool = toolForRadius(Math.max(minR, 0.01));
    issues.push(issue(
      "internal_radius", "warning", `Small Internal Radii (${smallRadii.length} corners)`,
      `${smallRadii.length} internal corner(s) with radius below ${MILLING_RULES.minInternalRadiusMm} mm. Smallest: R${minR.toFixed(2)} mm. ${tool ? `Requires Ø${tool.diameter} mm endmill.` : "No standard tool achieves this radius."}`,
      minR, MILLING_RULES.minInternalRadiusMm,
      smallRadii.flatMap(r => r.faceIndices).slice(0, 2000), smallRadii[0].centroid));
  }

  const cylinders = detectCylinders(context);
  const holes = cylinders.filter(c => c.concave && c.coverageRad >= FULL_CYLINDER_COVERAGE_RAD);
  const cavities = detectCavitiesLocal(context);
  const deepCavities = cavities.filter(c => c.depthRatio > MILLING_RULES.cavityDepthRatio);
  const holeCavities = holes.filter(h => h.diameterMm > 0 && h.depthMm / h.diameterMm > MILLING_RULES.cavityDepthRatio);
  const combined = deepCavities.length + holeCavities.length;
  if (combined > 0) {
    let maxRatio = MILLING_RULES.cavityDepthRatio;
    for (const c of deepCavities) maxRatio = Math.max(maxRatio, c.depthRatio);
    for (const h of holeCavities) maxRatio = Math.max(maxRatio, h.depthMm / h.diameterMm);
    const overlay = deepCavities.flatMap(c => c.faceIndices).concat(holeCavities.flatMap(h => h.faceIndices)).slice(0, 2000);
    issues.push(issue(
      "cavity_depth", maxRatio > 8 ? "error" : "warning", `Deep Cavities (${combined})`,
      `${combined} cavity/cavities exceed the ${MILLING_RULES.cavityDepthRatio}:1 depth/width limit. Worst: ${maxRatio.toFixed(1)}:1.`,
      maxRatio, MILLING_RULES.cavityDepthRatio, overlay,
      deepCavities[0]?.centroid || holeCavities[0]?.center || [0, 0, 0]));
  }

  const deepDrill = holes.filter(h => h.diameterMm > 0 && h.depthMm / h.diameterMm > MILLING_RULES.holeDepthTypicalRatio);
  if (deepDrill.length > 0) {
    const worst = Math.max(...deepDrill.map(h => h.depthMm / h.diameterMm));
    issues.push(issue(
      "hole", worst > MILLING_RULES.holeDepthFeasibleRatio ? "error" : "warning", `Deep Drill Holes (${deepDrill.length})`,
      `${deepDrill.length} hole(s) exceed the ${MILLING_RULES.holeDepthTypicalRatio}:1 depth/diameter drilling limit. Worst: ${worst.toFixed(1)}:1.`,
      worst, MILLING_RULES.holeDepthTypicalRatio,
      deepDrill.flatMap(h => h.faceIndices).slice(0, 2000), deepDrill[0].center));
  }

  const access = detectToolAccessLocal(context);
  if (access.minimumAxes > 3) {
    issues.push(issue(
      "tool_access", "info", `${access.minimumAxes}-Axis Machining Required`,
      access.details, access.minimumAxes, 3, access.inaccessible, [0, 0, 0]));
  }

  const undercuts = detectUndercutsLocal(context);
  if (undercuts.count > 0) {
    issues.push(issue(
      "undercut", "warning", `Undercuts (${undercuts.count})`,
      `${undercuts.count} undercut region(s) are shadowed from above and unreachable with a standard 3-axis setup. They need special tooling, a second setup, or a redesign.`,
      undercuts.count, 0, undercuts.faceIndices, undercuts.centroids[0] || []));
  }

  const chatter = detectChatterLocal(context);
  if (chatter.count > 0) {
    issues.push(issue(
      "chatter_risk", "warning", `Chatter Risk (${chatter.count} faces)`,
      `${chatter.count} large flat face(s) with thin support may vibrate during milling (chatter).`,
      chatter.count, 1, chatter.faceIndices, chatter.centroids[0] || []));
  }

  const sharp = detectSharpCornersLocal(context, MILLING_RULES.sharpCornerThresholdDeg);
  if (sharp.count > 0) {
    issues.push(issue(
      "sharp_corner", "warning", `Sharp Internal Corners (${sharp.count})`,
      `${sharp.count} sharp corner(s) may require EDM or are inaccessible to standard endmills.`,
      sharp.count, MILLING_RULES.sharpCornerThresholdDeg, sharp.faceIndices, sharp.centroids[0] || []));
  }
}

function analyzeCncTurning(context, issues) {
  const symmetry = detectAxialSymmetryLocal(context);
  if (!symmetry || !symmetry.isTurnable) {
    issues.push(issue(
      "not_turnable", "error", "Part Not Suitable for Turning",
      `Symmetry deviation ${(symmetry ? symmetry.deviation : 1).toFixed(3)} exceeds the turning threshold. The part likely requires milling.`,
      symmetry ? symmetry.deviation : 1, TURNING_RULES.symmetryThreshold));
    return;
  }
  if (symmetry.ldRatio != null && symmetry.ldRatio > TURNING_RULES.maxLengthDiameterRatio) {
    issues.push(issue(
      "ld_ratio", "warning", `High L/D Ratio (${symmetry.ldRatio.toFixed(1)}:1)`,
      `Length/diameter ratio ${symmetry.ldRatio.toFixed(1)} exceeds the recommended ${TURNING_RULES.maxLengthDiameterRatio}:1 limit. Steady rest or tailstock support required.`,
      symmetry.ldRatio, TURNING_RULES.maxLengthDiameterRatio));
  }
  const grooves = detectGroovesLocal(context, symmetry.axisIndex);
  const narrow = grooves.filter(g => g.widthMm < TURNING_RULES.minGrooveWidthMm && g.widthMm > 1e-6);
  if (narrow.length > 0) {
    const minW = Math.min(...narrow.map(g => g.widthMm));
    issues.push(issue(
      "groove", "warning", `Narrow Grooves (${narrow.length})`,
      `${narrow.length} groove(s) narrower than the ${TURNING_RULES.minGrooveWidthMm} mm minimum grooving tool width. Narrowest: ${minW.toFixed(2)} mm.`,
      minW, TURNING_RULES.minGrooveWidthMm));
  }
}

// --- Dispatcher ---------------------------------------------------------------

function normalizeProcessCode(processCode) {
  const normalized = String(processCode || "FDM").toUpperCase().replace(/-/g, "_");
  if (normalized === "CNC" || normalized.startsWith("CNC_MILL") || normalized === "MILLING") return "CNC_MILL";
  if (normalized === "CNC_TURN" || normalized === "TURNING" || normalized === "LATHE") return "CNC_TURN";
  if (normalized.startsWith("SHEET")) return "SHEET_METAL";
  if (normalized.includes("SILICONE") || normalized.includes("VACUUM_CAST")) return "SILICONE_CASTING";
  return normalized;
}

function buildIssues(mesh, metrics, processCode) {
  const issues = buildIntegrityIssues(metrics);
  if (metrics.isEmpty) {
    return issues;
  }

  const normalized = normalizeProcessCode(processCode);
  try {
    const context = buildDfmContext(mesh, metrics);
    if (PRINTING_RULES[normalized]) {
      analyzePrintingProcess(context, normalized, issues);
    } else if (normalized === "CNC_MILL") {
      analyzeCncMilling(context, issues);
    } else if (normalized === "CNC_TURN") {
      analyzeCncTurning(context, issues);
    } else if (normalized === "SHEET_METAL") {
      analyzeSheetMetalLocal(context, issues);
    } else if (normalized === "SILICONE_CASTING") {
      analyzeSiliconeCastingLocal(context, issues);
    }
  } catch (error) {
    issues.push(issue(
      "system", "info", "Partial analysis",
      `Process screening ended early (${error instanceof Error ? error.message : String(error)}); mesh integrity results above are still valid.`,
      0, 0));
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
