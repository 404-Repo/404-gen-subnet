// @expectedRule COMPUTED_PROPERTY_ACCESS
// Computed property access on THREE is forbidden.
export default function generate(THREE) {
  const type = 'BoxGeometry';
  const geo = new THREE[type](0.5, 0.5, 0.5);
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x22aa44 }));
  return mesh;
}
