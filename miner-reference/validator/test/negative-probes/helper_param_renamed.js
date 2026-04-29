// @expectedRule THREE_ALIAS_FORBIDDEN
// Passing THREE to a helper whose parameter is a plain identifier other than
// `THREE` used to rebind the whole namespace. The analyzer cannot track
// `t = THREE` symbolically, so the only safe policy is: helpers that accept
// THREE must use the exact parameter name `THREE`.
export default function generate(THREE) {
  const helper = (t) => {
    const { ShaderMaterial } = t;
    return new t.Mesh(
      new t.BoxGeometry(0.1, 0.1, 0.1),
      new ShaderMaterial({ vertexShader: '', fragmentShader: '' }),
    );
  };
  return helper(THREE);
}
