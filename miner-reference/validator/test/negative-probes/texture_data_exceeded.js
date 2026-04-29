// @expectedRule TEXTURE_DATA_EXCEEDED
// DataTexture with >4 MiB of pixel data (cap is 4 MiB).
export default function generate(THREE) {
  const w = 1100;
  const h = 1000;
  const data = new Uint8Array(w * h * 4);
  const tex = new THREE.DataTexture(data, w, h, THREE.RGBAFormat);

  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(0.2, 0.2, 0.2),
    new THREE.MeshStandardMaterial({ map: tex }),
  );
  return mesh;
}
