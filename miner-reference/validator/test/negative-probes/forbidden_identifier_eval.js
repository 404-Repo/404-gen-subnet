// @expectedRule FORBIDDEN_IDENTIFIER
// Uses `eval`, which is in the explicit forbidden-identifier deny list.
export default function generate(THREE) {
  const code = 'new THREE.BoxGeometry(1,1,1)';
  const geo = eval(code);
  return new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x0000ff }));
}
