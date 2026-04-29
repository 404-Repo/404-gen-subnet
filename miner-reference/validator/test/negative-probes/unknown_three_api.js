// @expectedRule UNKNOWN_THREE_API
// THREE.TorusKnotCurve is not a real member of the Three.js namespace.
// (TorusKnotGeometry exists, but there is no standalone TorusKnotCurve class.)
// The validator should distinguish "unknown / hallucinated" from "deliberately
// forbidden" — this rule code helps miners debug typos and hallucinations.
export default function generate(THREE) {
  const group = new THREE.Group();
  const curve = new THREE.TorusKnotCurve(0.3, 0.1, 100, 16);
  return group;
}
