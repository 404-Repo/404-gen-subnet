// @expectedRule THREE_ALIAS_FORBIDDEN
// Returning THREE from a helper is another way to launder it into a fresh
// binding the analyzer doesn't recognise. THREE may only appear in
// member access, a declarator init, an assignment right-hand side, a
// function argument, or a spread. Everywhere else is rejected.
export default function generate(THREE) {
  const leak = () => THREE;
  const X = leak();
  return new X.Mesh(new X.BoxGeometry(0.1, 0.1, 0.1), new X.MeshStandardMaterial());
}
