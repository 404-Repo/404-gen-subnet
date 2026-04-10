// @expectedRule FORBIDDEN_IDENTIFIER
// Math.random is explicitly blocked.
export default function generate(THREE) {
  const size = 0.1 + Math.random() * 0.9;
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(size, size, size),
    new THREE.MeshStandardMaterial({ color: 0x888888 }),
  );
  return mesh;
}
