// @expectedRule MISSING_DEFAULT_EXPORT
// No default export at all — only a named function.
function generate(THREE) {
  const group = new THREE.Group();
  group.add(
    new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.5, 0.5),
      new THREE.MeshStandardMaterial({ color: 0xff9900 }),
    ),
  );
  return group;
}
