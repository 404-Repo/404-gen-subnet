/**
 * A wireframe icosahedron drawn as line segments, demonstrating:
 *
 *   - `THREE.LineSegments` (the object type — pairs of vertices form lines)
 *   - `THREE.LineBasicMaterial` (the only material that renders lines
 *     correctly via gl.LINES draw mode)
 *   - `THREE.EdgesGeometry` (a built-in helper that extracts edges from any
 *     geometry as a non-indexed BufferGeometry suitable for LineSegments)
 *   - Per-vertex colors on a Line — color attribute on a LineSegments
 *     geometry colors each segment endpoint, with a smooth gradient between
 *
 * Visually: a glowing wireframe icosahedron with a height-based gradient
 * from cool blue at the bottom to warm orange at the top.
 */
// @vertices 60
// @drawCalls 1
// @maxDepth 1
// @instances 0
// @textureBytes 0

export default function generate(THREE) {
  const sourceGeometry = new THREE.IcosahedronGeometry(0.45, 0);
  const edgeGeometry = new THREE.EdgesGeometry(sourceGeometry);

  // Color each vertex by its Y position — cool blue at the bottom, warm
  // orange at the top. Gradient is computed from the position attribute.
  const positions = edgeGeometry.attributes.position;
  const colors = new Float32Array(positions.count * 3);
  for (let i = 0; i < positions.count; i++) {
    const y = positions.getY(i);
    const t = (y + 0.45) / 0.9; // normalize to [0, 1]
    colors[i * 3 + 0] = 0.2 + 0.8 * t;       // R: low → high
    colors[i * 3 + 1] = 0.4 + 0.3 * t;       // G: gentle
    colors[i * 3 + 2] = 0.9 - 0.6 * t;       // B: high → low
  }
  edgeGeometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

  const material = new THREE.LineBasicMaterial({
    vertexColors: true,
  });

  return new THREE.LineSegments(edgeGeometry, material);
}
