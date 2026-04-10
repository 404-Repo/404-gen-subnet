// @expectedRule IDENTIFIER_NOT_ALLOWED
// `console` is neither in the allowed-root list nor in the forbidden list,
// so it triggers the allowlist rejection (IDENTIFIER_NOT_ALLOWED).
export default function generate(THREE) {
  console.log('building scene');
  const group = new THREE.Group();
  group.add(
    new THREE.Mesh(
      new THREE.BoxGeometry(0.4, 0.4, 0.4),
      new THREE.MeshStandardMaterial({ color: 0xdddddd }),
    ),
  );
  return group;
}
