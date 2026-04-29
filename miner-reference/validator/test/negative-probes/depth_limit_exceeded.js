// @expectedRule DEPTH_LIMIT_EXCEEDED
// 33 levels of nested Groups, exceeding the depth-32 limit.
export default function generate(THREE) {
  const root = new THREE.Group();
  let current = root;
  for (let i = 0; i < 33; i++) {
    const child = new THREE.Group();
    current.add(child);
    current = child;
  }
  current.add(new THREE.Mesh(
    new THREE.BoxGeometry(0.1, 0.1, 0.1),
    new THREE.MeshStandardMaterial({ color: 0xcc8844 }),
  ));
  return root;
}
