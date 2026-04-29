// @expectedRule INVALID_RETURN_TYPE
// Returns a plain object instead of an Object3D.
export default function generate(THREE) {
  return { vertices: 100, faces: 50, type: 'CustomMesh' };
}
