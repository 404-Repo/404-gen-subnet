/**
 * A simple wooden dining chair: seat, backrest with vertical slats, 4 legs
 * with stretchers. Demonstrates a furniture-class output.
 */
// @vertices 832
// @drawCalls 17
// @maxDepth 1
// @instances 0
// @textureBytes 0

export default function generate(THREE) {
  const chair = new THREE.Group();

  const woodMat = new THREE.MeshStandardMaterial({
    color: 0x8b5a2b,
    metalness: 0.0,
    roughness: 0.7,
  });

  // Seat (slightly rounded edge via box for now)
  const seat = new THREE.Mesh(
    new THREE.BoxGeometry(1.0, 0.08, 1.0),
    woodMat,
  );
  seat.position.y = 1.0;
  chair.add(seat);

  // 4 legs
  const legGeo = new THREE.CylinderGeometry(0.05, 0.05, 1.0, 12);
  const legPositions = [
    [ 0.45, 0.5,  0.45],
    [-0.45, 0.5,  0.45],
    [ 0.45, 0.5, -0.45],
    [-0.45, 0.5, -0.45],
  ];
  for (const p of legPositions) {
    const leg = new THREE.Mesh(legGeo, woodMat);
    leg.position.set(p[0], p[1], p[2]);
    chair.add(leg);
  }

  // Stretchers (cross-bracing between legs at low height)
  const stretcherGeo = new THREE.CylinderGeometry(0.03, 0.03, 0.85, 8);
  const sFront = new THREE.Mesh(stretcherGeo, woodMat);
  sFront.position.set(0, 0.2, 0.45);
  sFront.rotation.z = Math.PI / 2;
  chair.add(sFront);
  const sBack = new THREE.Mesh(stretcherGeo, woodMat);
  sBack.position.set(0, 0.2, -0.45);
  sBack.rotation.z = Math.PI / 2;
  chair.add(sBack);
  const sLeft = new THREE.Mesh(stretcherGeo, woodMat);
  sLeft.position.set(-0.45, 0.2, 0);
  sLeft.rotation.x = Math.PI / 2;
  chair.add(sLeft);
  const sRight = new THREE.Mesh(stretcherGeo, woodMat);
  sRight.position.set(0.45, 0.2, 0);
  sRight.rotation.x = Math.PI / 2;
  chair.add(sRight);

  // Backrest frame (two vertical posts)
  const postGeo = new THREE.CylinderGeometry(0.04, 0.04, 1.0, 12);
  const postL = new THREE.Mesh(postGeo, woodMat);
  postL.position.set(-0.42, 1.55, -0.45);
  chair.add(postL);
  const postR = new THREE.Mesh(postGeo, woodMat);
  postR.position.set(0.42, 1.55, -0.45);
  chair.add(postR);

  // Top rail
  const topRail = new THREE.Mesh(
    new THREE.BoxGeometry(0.95, 0.08, 0.06),
    woodMat,
  );
  topRail.position.set(0, 2.05, -0.45);
  chair.add(topRail);

  // 5 vertical backrest slats
  const slatGeo = new THREE.BoxGeometry(0.06, 0.95, 0.04);
  for (let i = 0; i < 5; i++) {
    const slat = new THREE.Mesh(slatGeo, woodMat);
    slat.position.set(-0.32 + i * 0.16, 1.55, -0.45);
    chair.add(slat);
  }

  fitToUnitCube(THREE, chair);
  return chair;
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
