// @expectedRule ASYNC_NOT_ALLOWED
// Static analysis rejects an async default export function.
export default async function generate(THREE) {
  const group = new THREE.Group();
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(0.5, 0.5, 0.5),
    new THREE.MeshStandardMaterial({ color: 0x336699 }),
  );
  group.add(mesh);
  return group;
}
