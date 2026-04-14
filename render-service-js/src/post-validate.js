/**
 * Post-execution validation of a Three.js scene root.
 *
 * Implements the spec's bounded walk with cycle detection and computes all
 * required metrics: bounding box, vertex count, draw calls, depth, instance
 * count, and texture data bytes.
 *
 * Works in both Node.js (validation worker) and browser (defense-in-depth).
 * Receives THREE as parameter so it's context-agnostic.
 */

const LIMITS = {
  vertices: 250_000,
  drawCalls: 200,
  depth: 32,
  instances: 50_000,
  textureBytes: 1_048_576,
  bboxMin: -0.5,
  bboxMax: 0.5,
};

const MATERIAL_TEXTURE_KEYS = [
  'map', 'normalMap', 'roughnessMap', 'metalnessMap',
  'emissiveMap', 'aoMap', 'bumpMap', 'displacementMap',
  'alphaMap', 'lightMap', 'clearcoatMap', 'clearcoatNormalMap',
  'clearcoatRoughnessMap', 'sheenColorMap', 'sheenRoughnessMap',
  'transmissionMap', 'thicknessMap', 'specularIntensityMap',
  'specularColorMap', 'iridescenceMap', 'iridescenceThicknessMap',
  'anisotropyMap',
];

export function postValidate(THREE, root) {
  const failures = [];

  if (!root || !root.isObject3D) {
    failures.push({
      stage: 'post_validation',
      rule: 'INVALID_RETURN_TYPE',
      detail: root == null ? 'returned null/undefined' : 'not an Object3D',
    });
    return { passed: false, failures, metrics: null };
  }

  const validRoot = root.isGroup || root.isMesh || root.isLine || root.isPoints;
  if (!validRoot) {
    failures.push({
      stage: 'post_validation',
      rule: 'INVALID_RETURN_TYPE',
      detail: 'expected Group, Mesh, LineSegments, or Points',
    });
    return { passed: false, failures, metrics: null };
  }

  if (!root.isGroup) {
    const wrapper = new THREE.Group();
    wrapper.add(root);
    root = wrapper;
  }

  const visited = new Set();
  let vertices = 0;
  let drawCalls = 0;
  let maxDepth = 0;
  let instances = 0;
  const countedTextures = new Set();
  let textureBytes = 0;
  let walkAborted = false;

  function collectTextureBytes(material) {
    if (!material) return;
    const mats = Array.isArray(material) ? material : [material];
    for (const mat of mats) {
      for (const key of MATERIAL_TEXTURE_KEYS) {
        const tex = mat[key];
        if (!tex || !tex.isDataTexture || countedTextures.has(tex)) continue;
        countedTextures.add(tex);
        if (tex.image?.data?.byteLength) {
          textureBytes += tex.image.data.byteLength;
        }
      }
    }
  }

  function walk(node, depth) {
    if (walkAborted) return;

    if (visited.has(node)) {
      failures.push({
        stage: 'post_validation',
        rule: 'CYCLE_DETECTED',
        detail: 'scene graph contains a cycle',
      });
      walkAborted = true;
      return;
    }

    if (depth > LIMITS.depth) {
      failures.push({
        stage: 'post_validation',
        rule: 'DEPTH_LIMIT_EXCEEDED',
        detail: `${depth} > ${LIMITS.depth}`,
      });
      walkAborted = true;
      return;
    }

    visited.add(node);
    if (depth > maxDepth) maxDepth = depth;

    if (node.isMesh || node.isLine || node.isPoints) {
      drawCalls++;
      if (node.geometry?.attributes?.position) {
        vertices += node.geometry.attributes.position.count;
      }
      if (node.isInstancedMesh && node.count != null) {
        instances += node.count;
      }
      collectTextureBytes(node.material);
    }

    if (node.children) {
      for (const child of node.children) {
        walk(child, depth + 1);
        if (walkAborted) return;
      }
    }
  }

  walk(root, 0);
  if (walkAborted) {
    return { passed: false, failures, metrics: null };
  }

  const box = new THREE.Box3().setFromObject(root);
  if (box.isEmpty()) {
    failures.push({
      stage: 'post_validation',
      rule: 'EMPTY_SCENE',
      detail: 'bounding box is empty',
    });
  } else {
    const { min, max } = box;
    if (min.x < LIMITS.bboxMin || min.y < LIMITS.bboxMin || min.z < LIMITS.bboxMin ||
        max.x > LIMITS.bboxMax || max.y > LIMITS.bboxMax || max.z > LIMITS.bboxMax) {
      const axes = [];
      if (min.x < LIMITS.bboxMin || max.x > LIMITS.bboxMax) axes.push(`x:[${min.x.toFixed(4)},${max.x.toFixed(4)}]`);
      if (min.y < LIMITS.bboxMin || max.y > LIMITS.bboxMax) axes.push(`y:[${min.y.toFixed(4)},${max.y.toFixed(4)}]`);
      if (min.z < LIMITS.bboxMin || max.z > LIMITS.bboxMax) axes.push(`z:[${min.z.toFixed(4)},${max.z.toFixed(4)}]`);
      failures.push({
        stage: 'post_validation',
        rule: 'BOUNDING_BOX_OUT_OF_RANGE',
        detail: `out of [-0.5, 0.5]: ${axes.join(', ')}`,
      });
    }
  }

  if (vertices > LIMITS.vertices) {
    failures.push({
      stage: 'post_validation',
      rule: 'VERTEX_LIMIT_EXCEEDED',
      detail: `${vertices} > ${LIMITS.vertices}`,
    });
  }

  if (drawCalls > LIMITS.drawCalls) {
    failures.push({
      stage: 'post_validation',
      rule: 'DRAW_CALL_LIMIT_EXCEEDED',
      detail: `${drawCalls} > ${LIMITS.drawCalls}`,
    });
  }

  if (instances > LIMITS.instances) {
    failures.push({
      stage: 'post_validation',
      rule: 'INSTANCE_LIMIT_EXCEEDED',
      detail: `${instances} > ${LIMITS.instances}`,
    });
  }

  if (textureBytes > LIMITS.textureBytes) {
    failures.push({
      stage: 'post_validation',
      rule: 'TEXTURE_DATA_EXCEEDED',
      detail: `${textureBytes} > ${LIMITS.textureBytes}`,
    });
  }

  const { min, max } = box;
  const metrics = {
    vertices,
    drawCalls,
    depth: maxDepth,
    instances,
    textureBytes,
    bbox: box.isEmpty() ? null : {
      min: { x: min.x, y: min.y, z: min.z },
      max: { x: max.x, y: max.y, z: max.z },
    },
  };

  return { passed: failures.length === 0, failures, metrics };
}
