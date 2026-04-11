/**
 * A 3D point cloud with per-vertex colors, demonstrating:
 *
 *   - `THREE.Points` (the object type)
 *   - `THREE.PointsMaterial` (the only material type that renders points
 *     correctly — required because Points uses gl.POINTS draw mode and the
 *     mesh shaders don't set gl_PointSize)
 *   - Custom `THREE.BufferGeometry` constructed from scratch via
 *     `BufferAttribute`
 *   - Per-vertex colors via the `color` attribute and `vertexColors: true`
 *   - Deterministic Fibonacci-sphere distribution (no Math.random)
 *
 * Visually: a rainbow-colored sphere of 2000 dots, with color derived from
 * each point's position so the same point is always the same color.
 */
// @vertices 2000
// @drawCalls 1
// @maxDepth 1
// @instances 0
// @textureBytes 0

export default function generate(THREE) {
  const COUNT = 2000;
  const RADIUS = 0.45;

  const positions = new Float32Array(COUNT * 3);
  const colors = new Float32Array(COUNT * 3);

  // Fibonacci sphere — deterministic, evenly distributed, no Math.random.
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < COUNT; i++) {
    const y = 1 - (i / (COUNT - 1)) * 2; // -1 to 1
    const r = Math.sqrt(1 - y * y);
    const theta = goldenAngle * i;

    const x = RADIUS * r * Math.cos(theta);
    const z = RADIUS * r * Math.sin(theta);
    const yPos = RADIUS * y;

    positions[i * 3 + 0] = x;
    positions[i * 3 + 1] = yPos;
    positions[i * 3 + 2] = z;

    // Color from normalized position — gives a smooth rainbow shell.
    colors[i * 3 + 0] = (x + RADIUS) / (2 * RADIUS);
    colors[i * 3 + 1] = (yPos + RADIUS) / (2 * RADIUS);
    colors[i * 3 + 2] = (z + RADIUS) / (2 * RADIUS);
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

  const material = new THREE.PointsMaterial({
    size: 0.015,
    sizeAttenuation: true,
    vertexColors: true,
  });

  return new THREE.Points(geometry, material);
}
