// @expectedRule THREE_ALIAS_FORBIDDEN
// Same family as stash-on-this but using a plain object. Writing THREE
// onto any MemberExpression LHS is rejected — otherwise later
// dereferences like `obj.t.ShaderMaterial` would escape the allowlist.
export default function generate(THREE) {
  const obj = { t: null };
  obj.t = THREE;
  return new obj.t.Mesh(
    new obj.t.BoxGeometry(0.1, 0.1, 0.1),
    new obj.t.MeshStandardMaterial(),
  );
}
