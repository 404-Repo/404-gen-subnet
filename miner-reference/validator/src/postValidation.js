/**
 * Post-execution validation: walk the returned root, check every metric.
 *
 * Mirrors Runtime Spec § Post-Execution Validation. Single-pass bounded walk
 * with explicit cycle detection (Three.js's `Object3D.traverse` does not
 * detect cycles).
 */

import * as THREE from 'three';

export const LIMITS = {
  vertices: 250_000,
  drawCalls: 200,
  depth: 32,
  instances: 50_000,
  textureBytes: 4 * 1024 * 1024,
};

const ALLOWED_ROOT_TYPES = new Set([
  'Group',
  'Mesh',
  'LineSegments',
  'Points',
]);

// Standard PBR map slots that may reference a DataTexture.
const MAP_PROPS = [
  'map',
  'normalMap',
  'roughnessMap',
  'metalnessMap',
  'aoMap',
  'emissiveMap',
  'displacementMap',
  'bumpMap',
  'alphaMap',
  'envMap',
  'lightMap',
  'specularIntensityMap',
  'specularColorMap',
  'sheenRoughnessMap',
  'sheenColorMap',
  'transmissionMap',
  'thicknessMap',
  'clearcoatMap',
  'clearcoatNormalMap',
  'clearcoatRoughnessMap',
  'iridescenceMap',
  'iridescenceThicknessMap',
];

export function postValidate(root) {
  const failures = [];

  if (root === null || root === undefined) {
    failures.push({
      stage: 'post_validation',
      rule: 'INVALID_RETURN_TYPE',
      detail: 'returned null or undefined',
    });
    return { failures, metrics: null };
  }

  if (!root.isObject3D) {
    failures.push({
      stage: 'post_validation',
      rule: 'INVALID_RETURN_TYPE',
      detail: `returned ${typeof root}, not an Object3D`,
    });
    return { failures, metrics: null };
  }

  if (!ALLOWED_ROOT_TYPES.has(root.type)) {
    failures.push({
      stage: 'post_validation',
      rule: 'INVALID_RETURN_TYPE',
      detail: `returned ${root.type}, expected Group/Mesh/LineSegments/Points`,
    });
    return { failures, metrics: null };
  }

  // Auto-wrap bare Mesh / LineSegments / Points returns in a Group, matching
  // Output Spec § Return Value: "Single Mesh, LineSegments, or Points returns
  // are automatically wrapped in a Group by the validator."
  //
  // The wrap affects depth measurement (a bare Mesh now reports maxDepth 1
  // instead of 0) but does not change vertex count, draw calls, instance
  // count, texture data, or the bounding box — the Group wrapper has an
  // identity transform and contributes nothing of its own.
  let walkRoot = root;
  if (root.type !== 'Group') {
    walkRoot = new THREE.Group();
    walkRoot.add(root);
  }

  // Bounded walk with cycle detection.
  const visited = new Set();
  const seenTextures = new Set();
  let vertices = 0;
  let drawCalls = 0;
  let maxDepth = 0;
  let instances = 0;
  let textureBytes = 0;
  let cycleDetected = false;
  let depthExceeded = false;

  function walk(node, depth) {
    if (visited.has(node)) {
      cycleDetected = true;
      return;
    }
    if (depth > LIMITS.depth + 1) {
      depthExceeded = true;
      return;
    }
    visited.add(node);
    if (depth > maxDepth) maxDepth = depth;

    if (node.isMesh || node.isLine || node.isPoints) {
      drawCalls++;

      const geom = node.geometry;
      if (geom && geom.attributes && geom.attributes.position) {
        vertices += geom.attributes.position.count;
      }

      if (node.isInstancedMesh) {
        instances += node.count ?? 0;
      }

      const materials = Array.isArray(node.material)
        ? node.material
        : [node.material];
      for (const mat of materials) {
        if (!mat) continue;
        for (const key of MAP_PROPS) {
          const tex = mat[key];
          if (tex && tex.isDataTexture && !seenTextures.has(tex)) {
            seenTextures.add(tex);
            const data = tex.image && tex.image.data;
            if (data && typeof data.byteLength === 'number') {
              textureBytes += data.byteLength;
            }
          }
        }
      }
    }

    if (node.children) {
      for (const child of node.children) walk(child, depth + 1);
    }
  }

  walk(walkRoot, 0);

  if (cycleDetected) {
    failures.push({
      stage: 'post_validation',
      rule: 'CYCLE_DETECTED',
      detail: 'object hierarchy contains a cycle',
    });
    // Box3.setFromObject uses Three.js's traverse(), which has no cycle
    // detection and would infinitely recurse.  Return immediately.
    return {
      failures,
      metrics: { vertices, drawCalls, maxDepth, instances, textureBytes, bbox: null },
    };
  }
  if (depthExceeded) {
    failures.push({
      stage: 'post_validation',
      rule: 'DEPTH_LIMIT_EXCEEDED',
      detail: `walk exceeded max depth ${LIMITS.depth}`,
    });
  }

  if (vertices > LIMITS.vertices) {
    failures.push({
      stage: 'post_validation',
      rule: 'VERTEX_LIMIT_EXCEEDED',
      detail: `${vertices} vertices (limit ${LIMITS.vertices})`,
    });
  }
  if (drawCalls > LIMITS.drawCalls) {
    failures.push({
      stage: 'post_validation',
      rule: 'DRAW_CALL_LIMIT_EXCEEDED',
      detail: `${drawCalls} draw calls (limit ${LIMITS.drawCalls})`,
    });
  }
  if (maxDepth > LIMITS.depth) {
    failures.push({
      stage: 'post_validation',
      rule: 'DEPTH_LIMIT_EXCEEDED',
      detail: `max depth ${maxDepth} (limit ${LIMITS.depth})`,
    });
  }
  if (instances > LIMITS.instances) {
    failures.push({
      stage: 'post_validation',
      rule: 'INSTANCE_LIMIT_EXCEEDED',
      detail: `${instances} instances (limit ${LIMITS.instances})`,
    });
  }
  if (textureBytes > LIMITS.textureBytes) {
    failures.push({
      stage: 'post_validation',
      rule: 'TEXTURE_DATA_EXCEEDED',
      detail: `${textureBytes} bytes (limit ${LIMITS.textureBytes})`,
    });
  }

  // Bounding box — must fit inside [-0.5, 0.5] on all axes.
  // Computed from walkRoot (the post-wrap root) so the wrap's identity
  // transform is correctly applied — though for an identity-wrapped Mesh
  // the result is identical to setFromObject(root).
  let bbox = null;
  const box = new THREE.Box3().setFromObject(walkRoot);
  if (box.isEmpty()) {
    failures.push({
      stage: 'post_validation',
      rule: 'EMPTY_SCENE',
      detail: 'Box3.setFromObject(root).isEmpty() === true',
    });
  } else {
    bbox = {
      min: { x: box.min.x, y: box.min.y, z: box.min.z },
      max: { x: box.max.x, y: box.max.y, z: box.max.z },
    };
    if (
      box.min.x < -0.5 ||
      box.max.x > 0.5 ||
      box.min.y < -0.5 ||
      box.max.y > 0.5 ||
      box.min.z < -0.5 ||
      box.max.z > 0.5
    ) {
      failures.push({
        stage: 'post_validation',
        rule: 'BOUNDING_BOX_OUT_OF_RANGE',
        detail: `box [${fmt(box.min.x)}, ${fmt(box.min.y)}, ${fmt(box.min.z)}] to [${fmt(box.max.x)}, ${fmt(box.max.y)}, ${fmt(box.max.z)}]`,
      });
    }
  }

  return {
    failures,
    metrics: {
      vertices,
      drawCalls,
      maxDepth,
      instances,
      textureBytes,
      bbox,
    },
  };
}

function fmt(n) {
  return n.toFixed(3);
}
