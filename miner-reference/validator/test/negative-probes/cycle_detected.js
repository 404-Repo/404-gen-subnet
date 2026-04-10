// @expectedRule CYCLE_DETECTED
// Creates a cycle by directly pushing onto children, bypassing Object3D.add()
// which normally prevents cycles.
export default function generate(THREE) {
  const a = new THREE.Group();
  const b = new THREE.Group();
  a.add(new THREE.Mesh(
    new THREE.BoxGeometry(0.1, 0.1, 0.1),
    new THREE.MeshStandardMaterial({ color: 0xff0000 }),
  ));
  a.add(b);
  b.children.push(a);
  return a;
}
