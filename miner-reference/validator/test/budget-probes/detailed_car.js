/**
 * A "realistic-detail" car: rounded body via ExtrudeGeometry, beveled cabin,
 * wheels with rims, headlights, mirrors, exhaust. Roughly what a model output
 * for a recognizable vehicle would look like.
 */
// @vertices 2458
// @drawCalls 17
// @maxDepth 1
// @instances 0
// @textureBytes 0

export default function generate(THREE) {
  const car = new THREE.Group();

  const bodyMat = new THREE.MeshStandardMaterial({ color: 0xcc0000, metalness: 0.7, roughness: 0.3 });
  const trimMat = new THREE.MeshStandardMaterial({ color: 0x111111, metalness: 0.4, roughness: 0.6 });
  const glassMat = new THREE.MeshStandardMaterial({ color: 0x222244, metalness: 0.2, roughness: 0.1 });
  const tireMat = new THREE.MeshStandardMaterial({ color: 0x111111, metalness: 0.0, roughness: 0.95 });
  const rimMat = new THREE.MeshStandardMaterial({ color: 0xaaaaaa, metalness: 0.9, roughness: 0.2 });
  const lightMat = new THREE.MeshStandardMaterial({ color: 0xfff0cc, emissive: 0xfff0cc, emissiveIntensity: 0.5 });
  const exhaustMat = new THREE.MeshStandardMaterial({ color: 0x666666, metalness: 0.8, roughness: 0.4 });

  // Rounded body shell via ExtrudeGeometry from a 2D side profile
  const shape = new THREE.Shape();
  shape.moveTo(-2.0, 0.2);
  shape.lineTo(-1.8, 0.0);
  shape.lineTo( 1.8, 0.0);
  shape.lineTo( 2.0, 0.2);
  shape.lineTo( 2.0, 0.8);
  shape.lineTo( 1.2, 0.8);
  shape.lineTo( 0.8, 1.5);
  shape.lineTo(-0.6, 1.5);
  shape.lineTo(-1.0, 0.8);
  shape.lineTo(-2.0, 0.8);
  shape.lineTo(-2.0, 0.2);

  const body = new THREE.Mesh(
    new THREE.ExtrudeGeometry(shape, {
      depth: 1.6,
      bevelEnabled: true,
      bevelSize: 0.08,
      bevelThickness: 0.08,
      bevelSegments: 6,
      curveSegments: 24,
    }),
    bodyMat,
  );
  body.position.z = -0.8;
  car.add(body);

  // Windshield panel
  const wsGeo = new THREE.BoxGeometry(0.05, 0.6, 1.4);
  const ws = new THREE.Mesh(wsGeo, glassMat);
  ws.position.set(0.7, 1.15, 0);
  ws.rotation.z = -Math.PI / 4;
  car.add(ws);

  // Side trim strip
  const trimGeo = new THREE.BoxGeometry(4.0, 0.05, 0.05);
  const trimL = new THREE.Mesh(trimGeo, trimMat);
  trimL.position.set(0, 0.5, 0.81);
  car.add(trimL);
  const trimR = new THREE.Mesh(trimGeo, trimMat);
  trimR.position.set(0, 0.5, -0.81);
  car.add(trimR);

  // Wheels with rims (4 wheels = 4 tires + 4 rims = 8 meshes)
  const tireGeo = new THREE.CylinderGeometry(0.4, 0.4, 0.3, 24);
  const rimGeo = new THREE.CylinderGeometry(0.22, 0.22, 0.32, 16);
  const wheelPositions = [
    [ 1.3, 0,  0.9],
    [-1.3, 0,  0.9],
    [ 1.3, 0, -0.9],
    [-1.3, 0, -0.9],
  ];
  for (const p of wheelPositions) {
    const tire = new THREE.Mesh(tireGeo, tireMat);
    tire.position.set(p[0], p[1], p[2]);
    tire.rotation.z = Math.PI / 2;
    car.add(tire);

    const rim = new THREE.Mesh(rimGeo, rimMat);
    rim.position.set(p[0], p[1], p[2]);
    rim.rotation.z = Math.PI / 2;
    car.add(rim);
  }

  // Headlights (2 cones)
  const lightGeo = new THREE.SphereGeometry(0.12, 16, 12);
  const headL = new THREE.Mesh(lightGeo, lightMat);
  headL.position.set(2.0, 0.6, 0.5);
  car.add(headL);
  const headR = new THREE.Mesh(lightGeo, lightMat);
  headR.position.set(2.0, 0.6, -0.5);
  car.add(headR);

  // Side mirrors
  const mirrorGeo = new THREE.BoxGeometry(0.18, 0.1, 0.08);
  const mirL = new THREE.Mesh(mirrorGeo, trimMat);
  mirL.position.set(0.9, 1.1, 0.85);
  car.add(mirL);
  const mirR = new THREE.Mesh(mirrorGeo, trimMat);
  mirR.position.set(0.9, 1.1, -0.85);
  car.add(mirR);

  // Exhaust pipe
  const exhaustGeo = new THREE.CylinderGeometry(0.06, 0.06, 0.4, 12);
  const exhaust = new THREE.Mesh(exhaustGeo, exhaustMat);
  exhaust.position.set(-2.05, 0.2, -0.5);
  exhaust.rotation.z = Math.PI / 2;
  car.add(exhaust);

  fitToUnitCube(THREE, car);
  return car;
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
