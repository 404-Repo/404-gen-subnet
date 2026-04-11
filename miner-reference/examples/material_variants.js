/**
 * Three shapes side-by-side, each demonstrating one of the allowed mesh
 * material types:
 *
 *   - **MeshBasicMaterial** — unlit, ignores lighting entirely. The color
 *     is exactly what you set; useful for UI elements, helpers, and shapes
 *     that should look the same from every angle.
 *   - **MeshStandardMaterial** — the default PBR workhorse. Metalness +
 *     roughness, reacts to lights, looks right under most environments.
 *   - **MeshPhysicalMaterial** — extended PBR with clearcoat, transmission,
 *     iridescence, sheen. Use when you need a varnished/glossy/glass look
 *     that MeshStandardMaterial cannot produce.
 *
 * The three shapes are spaced along the X axis. Visually, you should see:
 *
 *   left   — flat unlit red box (no shading)
 *   center — metallic blue sphere with proper PBR highlights
 *   right  — translucent green torus with a clearcoat layer
 *
 * If your validator/renderer rejects any of these, the materials list in
 * the Output Spec is out of sync.
 */
// Vertex breakdown:
//   24  BoxGeometry (default segments)
// + 425 SphereGeometry(0.13, 24, 16) → (24+1)×(16+1)
// + 561 TorusGeometry(0.11, 0.04, 16, 32) → (32+1)×(16+1)
// = 1010
// @vertices 1010
// @drawCalls 3
// @maxDepth 1
// @instances 0
// @textureBytes 0

export default function generate(THREE) {
  const root = new THREE.Group();

  // Left: unlit basic material — looks the same regardless of lighting.
  const basic = new THREE.Mesh(
    new THREE.BoxGeometry(0.22, 0.22, 0.22),
    new THREE.MeshBasicMaterial({ color: 0xcc3030 }),
  );
  basic.position.x = -0.32;
  root.add(basic);

  // Center: standard PBR — metallic blue.
  const standard = new THREE.Mesh(
    new THREE.SphereGeometry(0.13, 24, 16),
    new THREE.MeshStandardMaterial({
      color: 0x3060cc,
      metalness: 0.85,
      roughness: 0.25,
    }),
  );
  root.add(standard);

  // Right: physical PBR — clearcoat layer over a green base.
  const physical = new THREE.Mesh(
    new THREE.TorusGeometry(0.11, 0.04, 16, 32),
    new THREE.MeshPhysicalMaterial({
      color: 0x30aa60,
      metalness: 0.0,
      roughness: 0.4,
      clearcoat: 1.0,
      clearcoatRoughness: 0.05,
    }),
  );
  physical.position.x = 0.32;
  root.add(physical);

  return root;
}
