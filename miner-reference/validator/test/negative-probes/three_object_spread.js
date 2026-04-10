// @expectedRule THREE_ALIAS_FORBIDDEN
// Object-spread copies every own enumerable property of THREE into `obj`,
// making every disallowed member reachable as `obj.X`.
export default function generate(THREE) {
  const obj = { ...THREE };
  return new obj.Mesh(new obj.BoxGeometry(0.1, 0.1, 0.1), new obj.MeshStandardMaterial());
}
