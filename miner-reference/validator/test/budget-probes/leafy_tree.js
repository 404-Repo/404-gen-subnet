/**
 * A tree: trunk + branches as cylinders + InstancedMesh leaves.
 * Tests the InstancedMesh constraint with realistic foliage density.
 */
// @vertices 488
// @drawCalls 8
// @maxDepth 1
// @instances 2000
// @textureBytes 0

export default function generate(THREE) {
  const tree = new THREE.Group();

  const barkMat = new THREE.MeshStandardMaterial({ color: 0x5a3a1f, metalness: 0.0, roughness: 0.9 });
  const leafMat = new THREE.MeshStandardMaterial({ color: 0x2d6a2f, metalness: 0.0, roughness: 0.7, side: THREE.DoubleSide });

  // Trunk
  const trunk = new THREE.Mesh(
    new THREE.CylinderGeometry(0.18, 0.28, 2.4, 16),
    barkMat,
  );
  trunk.position.y = 1.2;
  tree.add(trunk);

  // 6 main branches as cylinders
  const branchGeo = new THREE.CylinderGeometry(0.05, 0.1, 1.2, 10);
  for (let i = 0; i < 6; i++) {
    const angle = (i / 6) * Math.PI * 2;
    const branch = new THREE.Mesh(branchGeo, barkMat);
    branch.position.set(
      Math.cos(angle) * 0.4,
      2.0 + (i % 3) * 0.2,
      Math.sin(angle) * 0.4,
    );
    branch.rotation.z = -Math.cos(angle) * 0.7;
    branch.rotation.x = Math.sin(angle) * 0.7;
    tree.add(branch);
  }

  // 2,000 leaf instances arranged in a roughly spherical canopy
  const leafGeo = new THREE.PlaneGeometry(0.18, 0.18);
  const LEAVES = 2000;
  const leaves = new THREE.InstancedMesh(leafGeo, leafMat, LEAVES);
  const m = new THREE.Matrix4();
  const pos = new THREE.Vector3();
  const quat = new THREE.Quaternion();
  const scl = new THREE.Vector3(1, 1, 1);
  const e = new THREE.Euler();
  for (let i = 0; i < LEAVES; i++) {
    // Quasi-random positions inside an ellipsoid centered above the trunk
    const u = goldenSeq(i, 0);
    const v = goldenSeq(i, 1);
    const w = goldenSeq(i, 2);
    const theta = u * Math.PI * 2;
    const phi = Math.acos(2 * v - 1);
    const r = 0.7 + 0.3 * w;
    pos.set(
      r * Math.sin(phi) * Math.cos(theta) * 1.0,
      3.2 + r * Math.cos(phi) * 0.8,
      r * Math.sin(phi) * Math.sin(theta) * 1.0,
    );
    e.set(theta, phi, w * Math.PI);
    quat.setFromEuler(e);
    m.compose(pos, quat, scl);
    leaves.setMatrixAt(i, m);
  }
  leaves.instanceMatrix.needsUpdate = true;
  tree.add(leaves);

  fitToUnitCube(THREE, tree);
  return tree;
}

function goldenSeq(i, axis) {
  // Deterministic quasi-random sequence — no Math.random needed.
  const phi = [0.7548776662466927, 0.5698402909980532, 0.8005376432231778];
  const v = (i + 1) * phi[axis];
  return v - Math.floor(v);
}

function fitToUnitCube(THREE, root) {
  const box = new THREE.Box3().setFromObject(root);
  const size = new THREE.Vector3();
  const center = new THREE.Vector3();
  box.getSize(size);
  box.getCenter(center);
  const maxDim = Math.max(size.x, size.y, size.z);
  const scale = 0.95 / maxDim;
  root.scale.setScalar(scale);
  root.position.set(-center.x * scale, -center.y * scale, -center.z * scale);
}
