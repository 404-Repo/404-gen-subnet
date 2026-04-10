// @expectedRule THREE_ALIAS_FORBIDDEN
// Stashing THREE inside a container ({ t: THREE } / [THREE]) and reading it
// back out is a trivial bypass of the direct-alias rule. The parent-context
// whitelist on `THREE` identifiers catches both.
export default function generate(THREE) {
  const holder = { t: THREE };
  return new holder.t.Mesh(
    new holder.t.BoxGeometry(0.1, 0.1, 0.1),
    new holder.t.MeshStandardMaterial(),
  );
}
