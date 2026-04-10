// @expectedRule THREE_ALIAS_FORBIDDEN
// Full alias of THREE. Without this rule the analyzer only checks direct
// `THREE.X` member expressions, so `X.ShaderMaterial` (where X = THREE)
// would sneak past the allowlist.
export default function generate(THREE) {
  const X = THREE;
  return new X.Mesh(new X.BoxGeometry(0.1, 0.1, 0.1), new X.MeshStandardMaterial());
}
