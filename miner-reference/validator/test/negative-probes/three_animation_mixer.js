// @expectedRule FORBIDDEN_THREE_API
// THREE.AnimationMixer is not on the Three.js allowlist.
export default function generate(THREE) {
  const group = new THREE.Group();
  const mixer = new THREE.AnimationMixer(group);
  return group;
}
