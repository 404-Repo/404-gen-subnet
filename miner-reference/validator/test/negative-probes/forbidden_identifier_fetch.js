// @expectedRule FORBIDDEN_IDENTIFIER
// Uses `fetch`, which is in the explicit forbidden-identifier deny list.
export default function generate(THREE) {
  fetch('https://example.com/data.json');
  const group = new THREE.Group();
  group.add(
    new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.5, 0.5),
      new THREE.MeshStandardMaterial({ color: 0x00ff00 }),
    ),
  );
  return group;
}
