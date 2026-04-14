/**
 * Three.js scene renderer via Puppeteer.
 *
 * Each render request gets a fresh BrowserContext (isolated JS heap).
 * On failure the caller should restart the browser via browser.js.
 */

import { ensureBrowser, restartBrowser } from './browser.js';
import { STATIC_PORT } from './static-server.js';

const RENDER_TIMEOUT_MS = parseInt(process.env.RENDER_TIMEOUT_MS || '10000', 10);
const PAGE_URL = `http://127.0.0.1:${STATIC_PORT}/render-page.html`;

const DEFAULT_THETAS = [24.0, 120.0, 216.0, 312.0];
const DEFAULT_PHI = -15.0;
const DEFAULT_IMG_SIZE = 518;
const DEFAULT_CAM_RADIUS = 2.0;
const DEFAULT_CAM_FOV_DEG = 49.1;
const DEFAULT_GRID_GAP = 5;

/**
 * Render individual views. Returns an array of PNG Buffers.
 */
export async function renderViews(source, options = {}) {
  const params = buildParams(options);
  const dataUrls = await executeInContext(source, params, 'views');
  return dataUrls.map(dataUrlToBuffer);
}

/**
 * Render a composite grid image. Returns a single PNG Buffer.
 */
export async function renderGrid(source, options = {}) {
  const params = buildParams(options);
  const dataUrl = await executeInContext(source, params, 'grid');
  return dataUrlToBuffer(dataUrl);
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
  };
}

async function executeInContext(source, params, mode) {
  const browser = await ensureBrowser();
  const context = await browser.createBrowserContext();
  let page;
  try {
    page = await context.newPage();
    page.on('pageerror', (err) => console.error(`[browser error] ${err.message}`));
    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction('window.__ready === true', { timeout: 10_000 });

    const result = await withTimeout(
      page.evaluate(renderInPage, source, params, mode),
      RENDER_TIMEOUT_MS,
    );

    if (result.error) {
      throw new Error(result.error);
    }
    return result.data;
  } catch (err) {
    try { await context.close(); } catch {}
    await restartBrowser();
    throw err;
  } finally {
    try { await context.close(); } catch {}
  }
}

/**
 * Runs inside the Chromium page context.
 * Has access to window.THREE (set by render-page.html).
 */
async function renderInPage(source, params, mode) {
  try {
    const THREE = window.THREE;

    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      preserveDrawingBuffer: true,
      alpha: true,
    });
    renderer.setSize(params.imgSize, params.imgSize);
    renderer.setPixelRatio(1);
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.0;
    renderer.outputColorSpace = THREE.SRGBColorSpace;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(params.bgColor ? `#${params.bgColor}` : 0x808080);

    // Fixed studio lighting rig: lights stay in world space across all views
    // so shadows and shading are consistent for judge comparison.
    const ambient = new THREE.AmbientLight(0xffffff, 0.12);
    scene.add(ambient);

    const keyLight = new THREE.DirectionalLight(0xffffff, 1.2);
    keyLight.position.set(2, 3, 2);
    scene.add(keyLight);

    const fillLight = new THREE.DirectionalLight(0xffffff, 0.4);
    fillLight.position.set(-2, 1, 1);
    scene.add(fillLight);

    const rimLight = new THREE.DirectionalLight(0xffffff, 0.3);
    rimLight.position.set(0, 2, -3);
    scene.add(rimLight);

    // Neutral environment map for believable PBR reflections.
    const pmrem = new THREE.PMREMGenerator(renderer);
    const envScene = new THREE.Scene();
    envScene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.0));
    scene.environment = pmrem.fromScene(envScene, 0.04).texture;
    scene.environmentIntensity = 0.8;
    pmrem.dispose();

    const camera = new THREE.PerspectiveCamera(
      params.camFovDeg,
      1.0,
      0.01,
      100,
    );
    camera.up.set(0, 1, 0);

    const blob = new Blob([source], { type: 'application/javascript' });
    const blobUrl = URL.createObjectURL(blob);
    let mod;
    try {
      mod = await import(blobUrl);
    } finally {
      URL.revokeObjectURL(blobUrl);
    }

    if (typeof mod.default !== 'function') {
      return { error: 'default export is not a function' };
    }

    // Runtime guards: defense-in-depth against static-analysis bypasses.
    // Active only during mod.default(THREE); restored before we render.
    const _w = window;
    const _origRandom = Math.random;

    // Seeded PRNG (Mulberry32) so Three.js UUID generation still works.
    let _seed = 0x12345678;
    Math.random = () => {
      _seed |= 0; _seed = _seed + 0x6D2B79F5 | 0;
      let t = Math.imul(_seed ^ _seed >>> 15, 1 | _seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };

    const TRAPPED = [
      'eval', 'Function',
      'setTimeout', 'setInterval', 'setImmediate', 'queueMicrotask',
      'fetch', 'XMLHttpRequest', 'WebSocket',
      'document', 'window', 'navigator',
      'localStorage', 'sessionStorage', 'indexedDB',
      'OffscreenCanvas', 'HTMLCanvasElement',
      'crypto', 'Date', 'performance',
      'Proxy', 'Reflect',
      'WeakRef', 'FinalizationRegistry',
      'SharedArrayBuffer', 'Atomics',
      'Worker',
      'process', 'global', 'globalThis', 'self', 'require',
    ];
    const savedDescriptors = new Map();
    for (const name of TRAPPED) {
      if (!(name in _w)) continue;
      savedDescriptors.set(name, Object.getOwnPropertyDescriptor(_w, name));
      try {
        Object.defineProperty(_w, name, {
          get() { throw new Error(`Runtime violation: ${name} is forbidden`); },
          configurable: true,
        });
      } catch {
        try { _w[name] = undefined; } catch {}
      }
    }

    let root;
    try {
      root = mod.default(THREE);
    } finally {
      Math.random = _origRandom;
      for (const [name, desc] of savedDescriptors) {
        try {
          Object.defineProperty(_w, name, desc);
        } catch {
          try { _w[name] = desc?.value; } catch {}
        }
      }
    }

    if (!root || !root.isObject3D) {
      return { error: 'generate() did not return an Object3D' };
    }
    scene.add(root);

    function sphericalToCartesian(thetaDeg, phiDeg, radius) {
      const theta = thetaDeg * Math.PI / 180;
      const phi = phiDeg * Math.PI / 180;
      return new THREE.Vector3(
        radius * Math.cos(phi) * Math.sin(theta),
        -radius * Math.sin(phi),
        radius * Math.cos(phi) * Math.cos(theta),
      );
    }

    if (mode === 'grid') {
      const n = params.thetas.length;
      const cols = Math.ceil(Math.sqrt(n));
      const rows = Math.ceil(n / cols);
      const gap = params.gap;
      const gridW = cols * params.imgSize + (cols - 1) * gap;
      const gridH = rows * params.imgSize + (rows - 1) * gap;

      const gridCanvas = document.createElement('canvas');
      gridCanvas.width = gridW;
      gridCanvas.height = gridH;
      const ctx = gridCanvas.getContext('2d');
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, gridW, gridH);

      for (let i = 0; i < n; i++) {
        const pos = sphericalToCartesian(params.thetas[i], params.phis[i], params.camRadius);
        camera.position.copy(pos);
        camera.lookAt(0, 0, 0);

        renderer.render(scene, camera);

        const row = Math.floor(i / cols);
        const col = i % cols;
        ctx.drawImage(renderer.domElement, col * (params.imgSize + gap), row * (params.imgSize + gap));
      }

      renderer.dispose();
      return { data: gridCanvas.toDataURL('image/png') };
    }

    // Individual views
    const images = [];
    for (let i = 0; i < params.thetas.length; i++) {
      const pos = sphericalToCartesian(params.thetas[i], params.phis[i], params.camRadius);
      camera.position.copy(pos);
      camera.lookAt(0, 0, 0);

      renderer.render(scene, camera);
      images.push(renderer.domElement.toDataURL('image/png'));
    }

    renderer.dispose();
    return { data: images };
  } catch (err) {
    return { error: err.message || String(err) };
  }
}

function withTimeout(promise, ms) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`render timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function dataUrlToBuffer(dataUrl) {
  const base64 = dataUrl.replace(/^data:image\/png;base64,/, '');
  return Buffer.from(base64, 'base64');
}
