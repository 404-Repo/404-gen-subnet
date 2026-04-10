// @expectedRule FORBIDDEN_THREE_API
// The exact bypass the previous hardening pass left open: THREE flows into a
// helper whose parameter is an ObjectPattern that destructures disallowed
// members. The analyzer now inspects the callee's parameter shape at the
// call site and applies the same destructure rules that cover
// `const { ShaderMaterial } = THREE`.
export default function generate(THREE) {
  const use = ({ ShaderMaterial, Mesh, BoxGeometry }) => {
    return new Mesh(new BoxGeometry(0.1, 0.1, 0.1), new ShaderMaterial({
      vertexShader: '',
      fragmentShader: '',
    }));
  };
  return use(THREE);
}
