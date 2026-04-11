// @expectedRule INSTANCE_LIMIT_EXCEEDED
// InstancedMesh with count exceeding the 50,000 instance limit.
export default function generate(THREE) {
  const geo = new THREE.BoxGeometry(0.001, 0.001, 0.001);
  const mat = new THREE.MeshStandardMaterial({ color: 0x8888ff });
  const mesh = new THREE.InstancedMesh(geo, mat, 50001);
  return mesh;
}
