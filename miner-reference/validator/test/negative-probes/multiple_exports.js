// @expectedRule MULTIPLE_TOP_LEVEL_EXPORTS
// Two top-level exports: one named, one default.
export const SCALE = 2;

export default function generate(THREE) {
  const group = new THREE.Group();
  group.add(
    new THREE.Mesh(
      new THREE.BoxGeometry(0.3, 0.3, 0.3),
      new THREE.MeshStandardMaterial({ color: 0x9933cc }),
    ),
  );
  return group;
}
