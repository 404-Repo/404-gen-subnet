/**
 * A double helix of small cubes built from a single `InstancedMesh`,
 * demonstrating:
 *
 *   - `THREE.InstancedMesh` — one geometry + one material + many transforms
 *   - Per-instance transforms via `setMatrixAt`: translation AND rotation
 *   - `Matrix4.compose(position, quaternion, scale)` — the canonical way to
 *     build an instance transform
 *   - `instanceMatrix.needsUpdate = true` — required so the GPU sees the
 *     matrices after `setMatrixAt` has populated them
 *
 * Why instancing matters for the validator's caps: `InstancedMesh` counts as
 * **1 draw call** regardless of instance count, and the geometry's vertex
 * count is counted **once** (not multiplied). So 240 cubes here cost exactly
 * 1 draw call and 24 vertices in the budget, versus 240 separate meshes that
 * would cost 240 draw calls and 5,760 vertices. This is the pattern for any
 * scene with many repeated elements — forests, fences, particles, crowds,
 * brick walls, starfields, etc.
 *
 * Visually: two interleaved helices of blue cubes that wind around a central
 * axis. Each cube is rotated to face outward from the helix axis, so the
 * instance transform shows both translation and rotation at once.
 */
// @vertices 24
// @drawCalls 1
// @maxDepth 1
// @instances 240
// @textureBytes 0

export default function generate(THREE) {
  const PER_HELIX = 120;
  const INSTANCES = PER_HELIX * 2;

  // Single geometry + single material, shared across all 240 instances.
  const geometry = new THREE.BoxGeometry(0.05, 0.05, 0.05);
  const material = new THREE.MeshStandardMaterial({
    color: 0x4488cc,
    metalness: 0.5,
    roughness: 0.3,
  });

  const mesh = new THREE.InstancedMesh(geometry, material, INSTANCES);

  // Scratch objects reused in the loop to avoid allocating per-instance.
  const matrix = new THREE.Matrix4();
  const position = new THREE.Vector3();
  const quaternion = new THREE.Quaternion();
  const scale = new THREE.Vector3(1, 1, 1);
  const euler = new THREE.Euler();

  const radius = 0.3;
  const halfHeight = 0.4;
  const turns = 3;

  for (let i = 0; i < INSTANCES; i++) {
    // Two helices: even indices on the first, odd indices on the second,
    // offset by π so they interleave cleanly.
    const helixIndex = i % 2;
    const step = Math.floor(i / 2);
    const t = step / (PER_HELIX - 1); // 0 → 1 along each helix
    const angle = t * Math.PI * 2 * turns + helixIndex * Math.PI;

    position.set(
      radius * Math.cos(angle),
      -halfHeight + t * (2 * halfHeight),
      radius * Math.sin(angle),
    );

    // Rotate each cube so its +Z axis points away from the helix centerline.
    euler.set(0, -angle, 0);
    quaternion.setFromEuler(euler);

    matrix.compose(position, quaternion, scale);
    mesh.setMatrixAt(i, matrix);
  }

  // Required: without this flag, the GPU keeps using the default identity
  // matrices and every instance draws on top of the first one.
  mesh.instanceMatrix.needsUpdate = true;

  // Wrap in a Group so the example is explicitly about InstancedMesh, not
  // about bare-root auto-wrapping (covered by bare_mesh_root.js).
  const root = new THREE.Group();
  root.add(mesh);
  return root;
}
