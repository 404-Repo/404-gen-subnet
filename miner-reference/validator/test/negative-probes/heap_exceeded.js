// @expectedRule HEAP_EXCEEDED
// Allocates ~400 MB of regular JS objects to blow past the 256 MB heap cap.
// Note: ArrayBuffer / typed-array memory is external in V8 and would NOT trip
// the cap — must use regular JS objects to stress the V8 old generation.
export default function generate(THREE) {
  const ballast = [];
  for (let i = 0; i < 10_000_000; i++) {
    ballast.push({ a: i, b: i * 2, c: i * 3, d: i * 4 });
  }
  return new THREE.Group();
}
