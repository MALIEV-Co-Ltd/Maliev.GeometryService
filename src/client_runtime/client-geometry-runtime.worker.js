const MALIEV_BROWSER_GEOMETRY_RUNTIME_VERSION = "1.0.0";
const MALIEV_BROWSER_GEOMETRY_ALGORITHM_VERSION = "browser-first-dfm-v1";
const MALIEV_BROWSER_GEOMETRY_EXECUTION_MODE = "primary_interactive";

self.onmessage = async event => {
  const message = event.data || {};
  try {
    const result = await analyze(message.input || {}, message.processCode || "FDM");
    self.postMessage({ id: message.id || null, ok: true, result });
  } catch (error) {
    self.postMessage({
      id: message.id || null,
      ok: false,
      error: error instanceof Error ? error.message : String(error)
    });
  }
};

async function analyze(input, processCode) {
  const mesh = input.meshBuffers
    ? meshFromBuffers(input.meshBuffers)
    : meshFromFile(input.fileBytes, input.fileName || input.fileExtension || "");

  const metrics = computeMetrics(mesh);
  const inputHash = await hashMesh(mesh);
  const issues = buildIssues(metrics, processCode);

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
    metrics,
    issues,
    localOverlayHints: buildLocalOverlayHints(issues)
  };
}

function meshFromBuffers(buffers) {
  const positions = [];
  const indices = [];

  const sources = Array.isArray(buffers) ? buffers : [buffers];
  for (const source of sources) {
    const baseVertex = positions.length / 3;
    const sourcePositions = Array.from(source.positions || []);
    if (sourcePositions.length % 3 !== 0) {
      throw new Error("Mesh positions must be a flat XYZ array.");
    }
    positions.push(...sourcePositions);

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
  }

  return { positions, indices };
}

function meshFromFile(fileBytes, fileName) {
  if (!fileBytes) throw new Error("No mesh bytes were provided.");
  const bytes = fileBytes instanceof Uint8Array
    ? fileBytes
    : new Uint8Array(fileBytes);
  const lowerName = String(fileName || "").toLowerCase();
  if (lowerName.endsWith(".stl") || looksLikeStl(bytes)) {
    return parseStl(bytes);
  }
  throw new Error("Browser advisory runtime v1 supports STL bytes or viewer mesh buffers.");
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

function computeMetrics(mesh) {
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

  let area = 0;
  let signedVolume = 0;
  const edgeCounts = new Map();

  for (let index = 0; index < indices.length; index += 3) {
    const ia = indices[index] * 3;
    const ib = indices[index + 1] * 3;
    const ic = indices[index + 2] * 3;
    const a = [positions[ia], positions[ia + 1], positions[ia + 2]];
    const b = [positions[ib], positions[ib + 1], positions[ib + 2]];
    const c = [positions[ic], positions[ic + 1], positions[ic + 2]];
    const ab = subtract(b, a);
    const ac = subtract(c, a);
    const cross = crossProduct(ab, ac);
    area += vectorLength(cross) / 2;
    signedVolume += dot(a, crossProduct(b, c)) / 6;
    countEdge(edgeCounts, indices[index], indices[index + 1]);
    countEdge(edgeCounts, indices[index + 1], indices[index + 2]);
    countEdge(edgeCounts, indices[index + 2], indices[index]);
  }

  const nonManifoldEdgeCount = Array.from(edgeCounts.values()).filter(count => count !== 2).length;
  const extents = [max[0] - min[0], max[1] - min[1], max[2] - min[2]];

  return {
    vertexCount: positions.length / 3,
    faceCount: indices.length / 3,
    volumeMm3: Math.abs(signedVolume),
    surfaceAreaMm2: area,
    boundingBox: { x: extents[0], y: extents[1], z: extents[2] },
    isManifold: nonManifoldEdgeCount === 0,
    nonManifoldEdgeCount,
    isEmpty: indices.length === 0,
    complexity: complexityFor(indices.length / 3)
  };
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
    isEmpty: true,
    complexity: "empty"
  };
}

function buildIssues(metrics, processCode) {
  const issues = [];
  if (metrics.isEmpty) {
    issues.push(issue("system", "error", "Empty mesh", "No triangle geometry was available for local advisory analysis.", 0, 1));
  }
  if (!metrics.isManifold) {
    issues.push(issue("mesh_integrity", "warning", "Mesh may be non-manifold", "Local analysis found boundary or over-shared triangle edges. Server GeometryService remains authoritative.", metrics.nonManifoldEdgeCount, 0));
  }

  const minExtent = Math.min(metrics.boundingBox.x, metrics.boundingBox.y, metrics.boundingBox.z);
  const printing = ["FDM", "SLA", "SLS", "MJF", "MJ", "BJ", "DMLS", "SLA_DLP", "DLP"].includes(String(processCode).toUpperCase());
  if (printing && minExtent > 0 && minExtent < 0.8) {
    issues.push(issue("thin_wall", "warning", "Thin feature risk", "One model dimension is below the 0.8 mm local advisory threshold.", minExtent, 0.8));
  }

  return issues;
}

function issue(category, severity, title, description, value, threshold) {
  return { category, severity, title, description, value, threshold, faceIndices: [], centroid: [] };
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
