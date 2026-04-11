/**
 * Reference example: a low-poly car.
 *
 * This file is a hand-written example of what a miner's model is expected to
 * emit. It must satisfy every rule in `output_specifications.md` so that it
 * can also serve as a fixture for the validator and runtime.
 *
 * What it demonstrates:
 *   - Procedural geometry from primitives (BoxGeometry, CylinderGeometry)
 *   - MeshStandardMaterial with PBR properties (metalness, roughness)
 *   - A procedurally-generated DataTexture (no embedded image data)
 *   - A small Group hierarchy with positioned and rotated children
 *   - Auto-normalization to the [-0.5, 0.5] bounding box at the end, so
 *     the geometry is built at natural scale and only the root transform
 *     is adjusted to satisfy the bounding-box constraint
 *   - The forward axis convention (+Z), so the car points at the camera
 *
 * For coverage of the categories this file does NOT exercise (Points,
 * LineSegments, custom BufferGeometry, vertex colors, MeshPhysicalMaterial,
 * MeshBasicMaterial, PointsMaterial, LineBasicMaterial), see the other
 * fixtures in this directory.
 */
// @vertices 448
// @drawCalls 6
// @maxDepth 1
// @instances 0
// @textureBytes 1024

export default function generate(THREE) {
  const car = new THREE.Group();

  const bodyTexture = makeStripeTexture(THREE);

  const bodyMaterial = new THREE.MeshStandardMaterial({
    color: 0xcc0000,
    metalness: 0.6,
    roughness: 0.4,
    map: bodyTexture,
  });

  const cabinMaterial = new THREE.MeshStandardMaterial({
    color: 0x222244,
    metalness: 0.2,
    roughness: 0.3,
  });

  const wheelMaterial = new THREE.MeshStandardMaterial({
    color: 0x111111,
    metalness: 0.1,
    roughness: 0.8,
  });

  // Body — width along X, height along Y, length along Z (forward axis).
  const bodyGeometry = new THREE.BoxGeometry(2.0, 1.2, 4.0);
  const body = new THREE.Mesh(bodyGeometry, bodyMaterial);
  body.position.set(0, 0.6, 0);
  car.add(body);

  // Cabin sits on top of the body, slightly toward the rear (-Z).
  const cabinGeometry = new THREE.BoxGeometry(1.8, 1.0, 2.0);
  const cabin = new THREE.Mesh(cabinGeometry, cabinMaterial);
  cabin.position.set(0, 1.7, -0.3);
  car.add(cabin);

  // Wheels: 4 cylinders, each rotated so its axis lies along X. With the
  // axis along X, the wheel rolls along the Z (forward) axis.
  const wheelGeometry = new THREE.CylinderGeometry(0.4, 0.4, 0.3, 16);
  const wheelOffsets = [
    [ 1.1, 0,  1.3], // front-right
    [-1.1, 0,  1.3], // front-left
    [ 1.1, 0, -1.3], // rear-right
    [-1.1, 0, -1.3], // rear-left
  ];

  for (let i = 0; i < wheelOffsets.length; i++) {
    const wheel = new THREE.Mesh(wheelGeometry, wheelMaterial);
    const o = wheelOffsets[i];
    wheel.position.set(o[0], o[1], o[2]);
    wheel.rotation.z = Math.PI / 2;
    car.add(wheel);
  }

  // Build at natural scale, then scale + recenter the root Group so the
  // resulting bounding box exactly fits inside the unit cube. The validator
  // calls Box3.setFromObject(root), which respects the root's transform.
  fitToUnitCube(THREE, car);

  return car;
}

/**
 * Generate a small procedural stripe pattern as a DataTexture.
 * 16 × 16 RGBA = 1024 bytes — well under the 256 KB texture cap.
 */
function makeStripeTexture(THREE) {
  const size = 16;
  const data = new Uint8Array(size * size * 4);

  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      const onStripe = ((x >> 2) + (y >> 2)) % 2 === 0;
      data[i + 0] = onStripe ? 220 : 160;
      data[i + 1] = onStripe ? 30 : 10;
      data[i + 2] = onStripe ? 30 : 10;
      data[i + 3] = 255;
    }
  }

  const texture = new THREE.DataTexture(data, size, size);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.needsUpdate = true;
  return texture;
}

/**
 * Scale and translate `root` so that its bounding box fits inside the unit
 * cube [-1, 1]^3, centered at the origin, with a small margin to absorb
 * floating-point error.
 */
function fitToUnitCube(THREE, root) {
  const box = new THREE.Box3().setFromObject(root);
  const size = new THREE.Vector3();
  const center = new THREE.Vector3();
  box.getSize(size);
  box.getCenter(center);

  const maxDim = Math.max(size.x, size.y, size.z);
  // Target full extent: 0.95 (half-extent ≈ 0.475), leaving margin inside [-0.5, 0.5]
  const target = 0.95;
  const scale = target / maxDim;

  root.scale.setScalar(scale);
  root.position.set(
    -center.x * scale,
    -center.y * scale,
    -center.z * scale,
  );
}
