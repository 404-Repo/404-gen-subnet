// @expectedRule TEXTURE_DATA_EXCEEDED
// DataTexture with >1 MiB of pixel data.
export default function generate(THREE) {
  const w = 550;
  const h = 500;
  const data = new Uint8Array(w * h * 4);
  const tex = new THREE.DataTexture(data, w, h, THREE.RGBAFormat);

  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(0.2, 0.2, 0.2),
    new THREE.MeshStandardMaterial({ map: tex }),
  );
  return mesh;
}
