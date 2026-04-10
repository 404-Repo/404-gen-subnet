// @expectedRule THREE_AT_TOP_LEVEL
// References THREE outside the generate function body.
const defaultColor = THREE.Color;

export default function generate(THREE) {
  const group = new THREE.Group();
  group.add(
    new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.5, 0.5),
      new THREE.MeshStandardMaterial({ color: 0x446688 }),
    ),
  );
  return group;
}
