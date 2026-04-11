export default function generate(THREE) {
  const group = new THREE.Group();

  const balloonCenterY = 0.14;
  const knotCenterY = -0.20;

  const profile = [
    new THREE.Vector2(0, -0.325),
    new THREE.Vector2(0.04, -0.30),
    new THREE.Vector2(0.10, -0.26),
    new THREE.Vector2(0.18, -0.20),
    new THREE.Vector2(0.26, -0.12),
    new THREE.Vector2(0.33, -0.02),
    new THREE.Vector2(0.38, 0.06),
    new THREE.Vector2(0.40, 0.12),
    new THREE.Vector2(0.395, 0.18),
    new THREE.Vector2(0.37, 0.23),
    new THREE.Vector2(0.32, 0.27),
    new THREE.Vector2(0.24, 0.30),
    new THREE.Vector2(0.14, 0.32),
    new THREE.Vector2(0.05, 0.325),
    new THREE.Vector2(0, 0.325),
  ];

  const balloonGeo = new THREE.LatheGeometry(profile, 48);
  const balloonMat = new THREE.MeshPhysicalMaterial({
    color: 0x1E8FFF,
    clearcoat: 0.9,
    clearcoatRoughness: 0.05,
    roughness: 0.05,
    metalness: 0.0,
  });
  const balloonMesh = new THREE.Mesh(balloonGeo, balloonMat);
  balloonMesh.position.y = balloonCenterY;
  group.add(balloonMesh);

  const knotGeo = new THREE.SphereGeometry(0.018, 10, 8);
  knotGeo.scale(1.0, 1.5, 1.0);
  const knotMat = new THREE.MeshStandardMaterial({
    color: 0x1570CC,
    roughness: 0.3,
  });
  const knotMesh = new THREE.Mesh(knotGeo, knotMat);
  knotMesh.position.y = knotCenterY;
  group.add(knotMesh);

  const discGeo = new THREE.CylinderGeometry(0.025, 0.025, 0.004, 16);
  const discMat = new THREE.MeshPhysicalMaterial({
    color: 0x1E8FFF,
    transparent: true,
    opacity: 0.5,
    roughness: 0.2,
    metalness: 0.0,
  });
  const discMesh = new THREE.Mesh(discGeo, discMat);
  discMesh.position.y = knotCenterY - 0.02;
  group.add(discMesh);

  const curvePoints = [
    new THREE.Vector3(0, -0.225, 0),
    new THREE.Vector3(0.012, -0.27, 0.008),
    new THREE.Vector3(-0.010, -0.32, -0.005),
    new THREE.Vector3(0.015, -0.37, 0.010),
    new THREE.Vector3(-0.008, -0.42, -0.006),
    new THREE.Vector3(0.005, -0.465, 0.003),
  ];
  const curve = new THREE.CatmullRomCurve3(curvePoints);
  const stringGeo = new THREE.TubeGeometry(curve, 32, 0.003, 8, false);
  const stringMat = new THREE.MeshStandardMaterial({
    color: 0x4488FF,
    roughness: 0.6,
  });
  const stringMesh = new THREE.Mesh(stringGeo, stringMat);
  group.add(stringMesh);

  return group;
}
