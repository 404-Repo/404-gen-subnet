// @expectedRule THREE_ALIAS_FORBIDDEN
// Using THREE as a default parameter value binds the whole namespace to the
// parameter on any call where that argument is omitted. The `THREE`
// identifier appears as the right side of an AssignmentPattern — not an
// allowed parent context — so the reference itself is rejected.
export default function generate(THREE) {
  const helper = (t = THREE) =>
    new t.Mesh(new t.BoxGeometry(0.1, 0.1, 0.1), new t.MeshStandardMaterial());
  return helper();
}
