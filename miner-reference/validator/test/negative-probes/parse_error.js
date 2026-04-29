// @expectedRule PARSE_ERROR
// Deliberate syntax error: mismatched braces.
export default function generate(THREE) {
  const group = new THREE.Group(;
  return group;
}
