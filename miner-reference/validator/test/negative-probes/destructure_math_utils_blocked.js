// @expectedRule FORBIDDEN_THREE_API
// Destructuring a blocked submember (THREE.MathUtils.seededRandom) through
// a nested pattern. MathUtils itself is allowlisted but `seededRandom` is
// always blocked as a determinism violation.
export default function generate(THREE) {
  const { MathUtils: { seededRandom } } = THREE;
  const n = seededRandom ? 1 : 0;
  return new THREE.Mesh(new THREE.BoxGeometry(0.1 * n, 0.1, 0.1), new THREE.MeshStandardMaterial());
}
