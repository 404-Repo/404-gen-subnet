// @expectedRule BOUNDING_BOX_OUT_OF_RANGE
// Returns a valid scene, but the geometry is far larger than the ±0.5 bbox.
export default function generate(THREE) {
  const group = new THREE.Group();
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(5, 5, 5),
    new THREE.MeshStandardMaterial({ color: 0xff4444 }),
  );
  group.add(mesh);
  return group;
}
