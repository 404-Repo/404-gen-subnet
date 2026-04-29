/**
 * A square pyramid built entirely from a hand-rolled `BufferGeometry`,
 * demonstrating:
 *
 *   - `THREE.BufferGeometry` constructed from scratch (no primitive helper)
 *   - `THREE.BufferAttribute` for position, normal, and uv
 *   - Indexed geometry via `setIndex` — face indices reference shared
 *     vertices, halving vertex count vs the non-indexed equivalent
 *   - Manual normal computation per face (could also use
 *     `geometry.computeVertexNormals()`)
 *
 * The pyramid has 5 vertices: 4 base corners + 1 apex. 6 triangles
 * (2 for the square base + 4 for the sides). Indexed mesh:
 *   - 5 unique positions in the position attribute
 *   - 18 indices (6 triangles × 3 vertices each)
 *
 * This is the canonical pattern miners need when their inference pipeline
 * outputs raw vertex/face data instead of constructing from primitives.
 */
// @vertices 5
// @drawCalls 1
// @maxDepth 1
// @instances 0
// @textureBytes 0

export default function generate(THREE) {
  const h = 0.45; // pyramid height
  const s = 0.35; // half base width

  // 5 unique vertices: 4 base corners + 1 apex
  const positions = new Float32Array([
    -s, -h, -s,  // 0: base back-left
     s, -h, -s,  // 1: base back-right
     s, -h,  s,  // 2: base front-right
    -s, -h,  s,  // 3: base front-left
     0,  h,  0,  // 4: apex
  ]);

  // Per-vertex normals — averaged at the apex for shared smooth shading on
  // the slopes; the base normal points down. Real geometry would compute
  // these from face data, but for this demo we just spell them out.
  const normals = new Float32Array([
    0, -0.5, -0.5,  // 0: base back-left, partial down + back
    0, -0.5, -0.5,  // 1: base back-right
    0, -0.5,  0.5,  // 2: base front-right
    0, -0.5,  0.5,  // 3: base front-left
    0,  1,    0,    // 4: apex
  ]);

  // UV coordinates — base maps to corners of [0,1]², apex maps to center.
  const uvs = new Float32Array([
    0, 0,    // 0
    1, 0,    // 1
    1, 1,    // 2
    0, 1,    // 3
    0.5, 0.5, // 4
  ]);

  // Index buffer: 6 triangles = 18 indices.
  // Base (looking up at it from below, two triangles): 0-2-1, 0-3-2
  // Sides (each base edge + apex): 0-1-4, 1-2-4, 2-3-4, 3-0-4
  const indices = new Uint16Array([
    0, 2, 1,   0, 3, 2,
    0, 1, 4,
    1, 2, 4,
    2, 3, 4,
    3, 0, 4,
  ]);

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
  geometry.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));

  const material = new THREE.MeshStandardMaterial({
    color: 0xc28840,
    metalness: 0.3,
    roughness: 0.5,
  });

  return new THREE.Mesh(geometry, material);
}
