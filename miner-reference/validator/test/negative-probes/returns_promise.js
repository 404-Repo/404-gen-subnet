// @expectedRule ASYNC_NOT_ALLOWED
// The function is NOT async (passes static analysis), but the return value is
// a duck-typed thenable. The execution stage must detect the .then property
// and reject it. This is the fixture that would have caught the "awaited
// before thenable check" bug — if the runtime unwraps the Promise before
// inspecting the raw return, this fixture silently passes.
export default function generate(THREE) {
  const group = new THREE.Group();
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(0.4, 0.4, 0.4),
    new THREE.MeshStandardMaterial({ color: 0xff0000 }),
  );
  group.add(mesh);

  group.then = function (resolve) { resolve(group); };
  return group;
}
