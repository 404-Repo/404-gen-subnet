// @expectedRule DRAW_CALL_LIMIT_EXCEEDED
// Creates 201 meshes, exceeding the 200 draw-call limit.
export default function generate(THREE) {
  const group = new THREE.Group();
  const geo = new THREE.BoxGeometry(0.01, 0.01, 0.01);
  const mat = new THREE.MeshStandardMaterial({ color: 0x44cc88 });
  for (let i = 0; i < 201; i++) {
    const m = new THREE.Mesh(geo, mat);
    m.position.set((i % 15) * 0.02 - 0.15, Math.floor(i / 15) * 0.02 - 0.15, 0);
    group.add(m);
  }
  return group;
}
