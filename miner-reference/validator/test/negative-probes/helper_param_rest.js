// @expectedRule THREE_ALIAS_FORBIDDEN
// Passing THREE into a helper with a rest parameter is another escape hatch
// (args[0] then carries THREE). Rest params that could capture THREE are
// rejected at the call site.
export default function generate(THREE) {
  const helper = (...args) => {
    const t = args[0];
    return new t.Mesh(new t.BoxGeometry(0.1, 0.1, 0.1), new t.MeshStandardMaterial());
  };
  return helper(THREE);
}
