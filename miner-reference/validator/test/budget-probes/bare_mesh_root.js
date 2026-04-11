/**
 * Returns a single Mesh as the root, not a Group.
 *
 * Per Output Spec § Return Value, the validator auto-wraps bare Mesh /
 * LineSegments / Points returns in a Group. This probe locks in that
 * behavior — it must PASS validation, and `maxDepth` must be 1 (Group
 * wrapper at depth 0, Mesh at depth 1), never 0. If anyone removes the
 * wrap in the future, the @maxDepth annotation below will catch it as a
 * metric drift even though validation would still nominally pass.
 */
// @vertices 24
// @drawCalls 1
// @maxDepth 1
// @instances 0
// @textureBytes 0

export default function generate(THREE) {
  return new THREE.Mesh(
    new THREE.BoxGeometry(0.4, 0.4, 0.4),
    new THREE.MeshStandardMaterial({
      color: 0x4488cc,
      metalness: 0.5,
      roughness: 0.4,
    }),
  );
}
