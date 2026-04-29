/**
 * A small Gothic-ish cathedral: multiple naves, towers, buttresses, windows,
 * spires. Stress test for draw-call count: separate meshes for every
 * architectural feature.
 */
// @vertices 1497
// @drawCalls 39
// @maxDepth 1
// @instances 0
// @textureBytes 0

export default function generate(THREE) {
  const cathedral = new THREE.Group();

  const stoneMat = new THREE.MeshStandardMaterial({ color: 0xb8b0a0, metalness: 0.0, roughness: 0.85 });
  const roofMat = new THREE.MeshStandardMaterial({ color: 0x5a3a2f, metalness: 0.1, roughness: 0.7 });
  const glassMat = new THREE.MeshStandardMaterial({ color: 0x4a4090, metalness: 0.2, roughness: 0.1, emissive: 0x101030, emissiveIntensity: 0.4 });
  const goldMat = new THREE.MeshStandardMaterial({ color: 0xc0a040, metalness: 0.9, roughness: 0.2 });

  // Main nave
  const nave = new THREE.Mesh(new THREE.BoxGeometry(2.4, 1.8, 6.0), stoneMat);
  nave.position.y = 0.9;
  cathedral.add(nave);

  // Nave roof (4-sided pyramid)
  const naveRoof = new THREE.Mesh(new THREE.ConeGeometry(1.7, 0.8, 4), roofMat);
  naveRoof.position.y = 2.2;
  naveRoof.rotation.y = Math.PI / 4;
  cathedral.add(naveRoof);

  // Transept (cross-arms)
  const transept = new THREE.Mesh(new THREE.BoxGeometry(4.0, 1.6, 1.6), stoneMat);
  transept.position.set(0, 0.8, 0);
  cathedral.add(transept);

  // 2 main towers at the front
  for (const x of [-1.4, 1.4]) {
    const tower = new THREE.Mesh(new THREE.BoxGeometry(1.2, 3.2, 1.2), stoneMat);
    tower.position.set(x, 1.6, 2.6);
    cathedral.add(tower);

    const spire = new THREE.Mesh(new THREE.ConeGeometry(0.85, 1.6, 4), roofMat);
    spire.position.set(x, 4.0, 2.6);
    spire.rotation.y = Math.PI / 4;
    cathedral.add(spire);

    const cross = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.5, 0.06), goldMat);
    cross.position.set(x, 5.0, 2.6);
    cathedral.add(cross);
    const crossbar = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.06, 0.06), goldMat);
    crossbar.position.set(x, 5.05, 2.6);
    cathedral.add(crossbar);
  }

  // 12 buttresses along the nave (6 per side)
  const buttressGeo = new THREE.BoxGeometry(0.3, 1.6, 0.5);
  for (let i = 0; i < 6; i++) {
    const z = -2.4 + i * 0.96;
    const left = new THREE.Mesh(buttressGeo, stoneMat);
    left.position.set(-1.4, 0.8, z);
    cathedral.add(left);
    const right = new THREE.Mesh(buttressGeo, stoneMat);
    right.position.set(1.4, 0.8, z);
    cathedral.add(right);
  }

  // 12 stained-glass windows (6 per side)
  const windowGeo = new THREE.BoxGeometry(0.04, 0.9, 0.4);
  for (let i = 0; i < 6; i++) {
    const z = -2.2 + i * 0.96;
    const left = new THREE.Mesh(windowGeo, glassMat);
    left.position.set(-1.21, 1.0, z);
    cathedral.add(left);
    const right = new THREE.Mesh(windowGeo, glassMat);
    right.position.set(1.21, 1.0, z);
    cathedral.add(right);
  }

  // Rose window above the entrance (a torus + 8 spokes)
  const rose = new THREE.Mesh(new THREE.TorusGeometry(0.35, 0.04, 8, 32), goldMat);
  rose.position.set(0, 2.2, 3.21);
  cathedral.add(rose);
  const roseGlass = new THREE.Mesh(new THREE.CircleGeometry(0.34, 24), glassMat);
  roseGlass.position.set(0, 2.2, 3.21);
  cathedral.add(roseGlass);

  // Entrance arch
  const arch = new THREE.Mesh(new THREE.BoxGeometry(0.7, 1.2, 0.06), goldMat);
  arch.position.set(0, 0.8, 3.21);
  cathedral.add(arch);

  // Apse (rounded back)
  const apse = new THREE.Mesh(new THREE.SphereGeometry(1.2, 24, 12, 0, Math.PI * 2, 0, Math.PI / 2), stoneMat);
  apse.position.set(0, 0.8, -3.0);
  apse.rotation.x = -Math.PI / 2;
  cathedral.add(apse);

  fitToUnitCube(THREE, cathedral);
  return cathedral;
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
