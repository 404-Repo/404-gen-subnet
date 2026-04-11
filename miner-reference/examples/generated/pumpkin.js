export default function generate(THREE) {
  const group = new THREE.Group();

  const texSize = 256;
  const texData = new Uint8Array(texSize * texSize * 4);
  for (let i = 0; i < texSize * texSize; i++) {
    const px = i % texSize;
    const py = (i / texSize) | 0;
    let h = (px * 374761393 + py * 668265263) | 0;
    h = ((h ^ (h >>> 13)) * 1274126177) | 0;
    h = (h ^ (h >>> 16)) & 0xFFFF;
    const idx = i * 4;
    if (h < 250) {
      texData[idx] = 255;
      texData[idx + 1] = 238;
      texData[idx + 2] = 200;
      texData[idx + 3] = 255;
    } else {
      texData[idx] = 232;
      texData[idx + 1] = 120;
      texData[idx + 2] = 0;
      texData[idx + 3] = 255;
    }
  }
  const speckleTex = new THREE.DataTexture(texData, texSize, texSize, THREE.RGBAFormat);
  speckleTex.needsUpdate = true;

  const pumpkinMat = new THREE.MeshStandardMaterial({
    map: speckleTex,
    roughness: 0.65,
    metalness: 0.05
  });

  const lobeGeo = new THREE.SphereGeometry(1, 20, 14);
  const lobeCount = 10;
  for (let i = 0; i < lobeCount; i++) {
    const angle = (i / lobeCount) * Math.PI * 2;
    const lobe = new THREE.Mesh(lobeGeo, pumpkinMat);
    lobe.scale.set(0.24, 0.23, 0.105);
    lobe.position.set(Math.cos(angle) * 0.14, 0, Math.sin(angle) * 0.14);
    lobe.rotation.y = -angle;
    group.add(lobe);
  }

  const coreGeo = new THREE.SphereGeometry(1, 12, 10);
  const core = new THREE.Mesh(coreGeo, pumpkinMat);
  core.scale.set(0.13, 0.20, 0.13);
  group.add(core);

  const stemMat = new THREE.MeshStandardMaterial({
    color: 0x2E8B2E,
    roughness: 0.7,
    metalness: 0.05
  });

  const collarGeo = new THREE.CylinderGeometry(0.033, 0.042, 0.015, 10);
  const collar = new THREE.Mesh(collarGeo, stemMat);
  collar.position.y = 0.19;
  group.add(collar);

  const stemGeo = new THREE.CylinderGeometry(0.015, 0.028, 0.11, 8);
  const stem = new THREE.Mesh(stemGeo, stemMat);
  stem.position.set(0.005, 0.253, 0);
  stem.rotation.z = 0.15;
  group.add(stem);

  const box = new THREE.Box3().setFromObject(group);
  const size = new THREE.Vector3();
  box.getSize(size);
  const center = new THREE.Vector3();
  box.getCenter(center);
  const maxDim = Math.max(size.x, size.y, size.z);
  const s = 0.95 / maxDim;

  const root = new THREE.Group();
  root.add(group);
  group.position.set(-center.x, -center.y, -center.z);
  root.scale.setScalar(s);

  return root;
}
