// @expectedRule VERTEX_LIMIT_EXCEEDED
// High-resolution TorusKnotGeometry produces >250k vertices.
// (2049 * 129 = 264,321 verts; radius + tube fits within ±0.5 bbox)
export default function generate(THREE) {
  const mesh = new THREE.Mesh(
    new THREE.TorusKnotGeometry(0.3, 0.05, 2048, 128),
    new THREE.MeshStandardMaterial({ color: 0x4488cc }),
  );
  return mesh;
}
