// @expectedRule UNKNOWN_THREE_API
// Destructuring a name that isn't actually a member of the Three.js
// namespace should be caught with UNKNOWN_THREE_API, the same way the
// direct `THREE.TorusKnotCurve` member access is.
export default function generate(THREE) {
  const { TorusKnotCurve } = THREE;
  return new THREE.Group();
}
