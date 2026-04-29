// @expectedRule FORBIDDEN_THREE_API
// THREE.TextureLoader is not on the Three.js allowlist.
export default function generate(THREE) {
  const loader = new THREE.TextureLoader();
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(0.5, 0.5, 0.5),
    new THREE.MeshStandardMaterial({ color: 0xcccccc }),
  );
  return mesh;
}
