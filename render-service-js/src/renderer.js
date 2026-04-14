/**
 * Three.js scene renderer — public API.
 *
 * Thin wrapper that builds render params from caller options and
 * delegates to the render pool for bounded-concurrency execution.
 */

import { renderPool } from './render-pool.js';

const DEFAULT_THETAS = [24.0, 120.0, 216.0, 312.0];
const DEFAULT_PHI = -15.0;
const DEFAULT_IMG_SIZE = 518;
const DEFAULT_CAM_RADIUS = 2.0;
const DEFAULT_CAM_FOV_DEG = 49.1;
const DEFAULT_GRID_GAP = 5;

export async function renderViews(source, options = {}) {
  return renderPool.renderViews(source, buildParams(options));
}

export async function renderGrid(source, options = {}) {
  return renderPool.renderGrid(source, buildParams(options));
}

function buildParams(options) {
  const thetas = options.thetas || DEFAULT_THETAS;
  let phis = options.phis;
  if (phis == null) {
    phis = Array(thetas.length).fill(DEFAULT_PHI);
  } else if (typeof phis === 'number') {
    phis = Array(thetas.length).fill(phis);
  }
  return {
    thetas,
    phis,
    imgSize: options.imgSize || DEFAULT_IMG_SIZE,
    camRadius: options.camRadius || DEFAULT_CAM_RADIUS,
    camFovDeg: options.camFovDeg || DEFAULT_CAM_FOV_DEG,
    gap: options.gap ?? DEFAULT_GRID_GAP,
    bgColor: options.bgColor || null,
    lighting: options.lighting || 'studio',
  };
}
