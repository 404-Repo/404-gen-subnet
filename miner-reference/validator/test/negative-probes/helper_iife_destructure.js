// @expectedRule FORBIDDEN_THREE_API
// IIFE variant of the destructured-param bypass. The callee is an inline
// arrow function, which the analyzer still resolves directly.
export default function generate(THREE) {
  return (({ Mesh, BoxGeometry, ShaderMaterial }) =>
    new Mesh(
      new BoxGeometry(0.1, 0.1, 0.1),
      new ShaderMaterial({ vertexShader: '', fragmentShader: '' }),
    ))(THREE);
}
