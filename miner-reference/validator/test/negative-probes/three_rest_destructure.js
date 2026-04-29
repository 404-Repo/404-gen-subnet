// @expectedRule THREE_ALIAS_FORBIDDEN
// Rest-destructure captures every THREE member, including every disallowed
// one, into a local object — defeating the allowlist entirely.
export default function generate(THREE) {
  const { ...rest } = THREE;
  return new rest.Mesh(new rest.BoxGeometry(0.1, 0.1, 0.1), new rest.MeshStandardMaterial());
}
