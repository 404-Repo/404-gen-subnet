// @expectedRule FORBIDDEN_THREE_API
// Destructuring pulls a disallowed THREE member into scope. The resulting
// local binding `ShaderMaterial` would satisfy the identifier allowlist
// check (it has a binding) — so the analyzer must reject at the
// destructure site itself, not later at the use site.
export default function generate(THREE) {
  const { ShaderMaterial } = THREE;
  const mat = new ShaderMaterial({ vertexShader: '', fragmentShader: '' });
  return new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), mat);
}
