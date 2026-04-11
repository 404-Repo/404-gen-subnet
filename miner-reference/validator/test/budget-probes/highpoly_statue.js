/**
 * A "high-detail" smooth statue: a TorusKnot with high segment counts as a
 * stand-in for a procedurally-sculpted hero asset. Tests how close to the
 * vertex cap a single hero mesh can get.
 */
// @vertices 25221
// @drawCalls 2
// @maxDepth 1
// @instances 0
// @textureBytes 0

export default function generate(THREE) {
  const root = new THREE.Group();

  const mat = new THREE.MeshStandardMaterial({
    color: 0xc0a060,
    metalness: 0.85,
    roughness: 0.25,
  });

  // High-segment torus knot — a single ~75k-vertex hero mesh.
  const knot = new THREE.Mesh(
    new THREE.TorusKnotGeometry(0.6, 0.18, 384, 64, 2, 3),
    mat,
  );
  root.add(knot);

  // A pedestal under it
  const pedestal = new THREE.Mesh(
    new THREE.CylinderGeometry(0.5, 0.55, 0.15, 32),
    new THREE.MeshStandardMaterial({ color: 0x222222, metalness: 0.1, roughness: 0.8 }),
  );
  pedestal.position.y = -0.85;
  root.add(pedestal);

  fitToUnitCube(THREE, root);
  return root;
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
