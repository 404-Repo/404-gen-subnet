/**
 * Render slot pool.
 *
 * Manages bounded concurrency for browser-based rendering. Each render
 * gets a fresh BrowserContext. On failure only that context is closed;
 * the browser is restarted only if the process itself died.
 *
 * Pool size: RENDER_POOL_SIZE env var (default 2).
 */

import { ensureBrowser, isBrowserHealthy, restartBrowser } from './browser.js';
import { STATIC_PORT } from './static-server.js';

const RENDER_TIMEOUT_MS = parseInt(process.env.RENDER_TIMEOUT_MS || '10000', 10);
const POOL_SIZE = parseInt(process.env.RENDER_POOL_SIZE || '2', 10);
const PAGE_URL = `http://127.0.0.1:${STATIC_PORT}/render-page.html`;

class RenderPool {
  constructor() {
    this._available = POOL_SIZE;
    this._waiting = [];
  }

  async renderViews(source, params) {
    return this._withSlot(async () => {
      const dataUrls = await this._executeInContext(source, params, 'views');
      return dataUrls.map(dataUrlToBuffer);
    });
  }

  async renderGrid(source, params) {
    return this._withSlot(async () => {
      const dataUrl = await this._executeInContext(source, params, 'grid');
      return dataUrlToBuffer(dataUrl);
    });
  }

  async _withSlot(fn) {
    await this._acquire();
    try {
      return await fn();
    } finally {
      this._release();
    }
  }

  _acquire() {
    if (this._available > 0) {
      this._available--;
      return Promise.resolve();
    }
    return new Promise((resolve) => this._waiting.push(resolve));
  }

  _release() {
    if (this._waiting.length > 0) {
      this._waiting.shift()();
    } else {
      this._available++;
    }
  }

  async _executeInContext(source, params, mode) {
    if (!isBrowserHealthy()) {
      console.log('[render-pool] browser not healthy, restarting...');
      await restartBrowser();
    }

    const browser = await ensureBrowser();
    const context = await browser.createBrowserContext();
    let page;
    try {
      page = await context.newPage();
      page.on('pageerror', (err) => console.error(`[browser error] ${err.message}`));
      await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });
      await page.waitForFunction('window.__ready === true', { timeout: 10_000 });
      await page.setOfflineMode(true);

      const result = await withTimeout(
        page.evaluate(renderInPage, source, params, mode),
        RENDER_TIMEOUT_MS,
      );

      if (result.error) throw new Error(result.error);
      return result.data;
    } catch (err) {
      try { await context.close(); } catch {}
      if (!isBrowserHealthy()) {
        console.log('[render-pool] browser died during render, will restart on next request');
      }
      throw err;
    } finally {
      try { await context.close(); } catch {}
    }
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

    let followLight = null;

    if (params.lighting === 'follow') {
      scene.add(new THREE.AmbientLight(0xffffff, 0.4));
      followLight = new THREE.DirectionalLight(0xffffff, 1.5);
      scene.add(followLight);
    } else {
      scene.add(new THREE.AmbientLight(0xffffff, 0.12));

      const keyLight = new THREE.DirectionalLight(0xffffff, 1.2);
      keyLight.position.set(2, 3, 2);
      scene.add(keyLight);

      const fillLight = new THREE.DirectionalLight(0xffffff, 0.4);
      fillLight.position.set(-2, 1, 1);
      scene.add(fillLight);

      const rimLight = new THREE.DirectionalLight(0xffffff, 0.3);
      rimLight.position.set(0, 2, -3);
      scene.add(rimLight);

      const pmrem = new THREE.PMREMGenerator(renderer);
      const envScene = new THREE.Scene();
      envScene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.0));
      scene.environment = pmrem.fromScene(envScene, 0.04).texture;
      scene.environmentIntensity = 0.8;
      pmrem.dispose();
    }

    const camera = new THREE.PerspectiveCamera(params.camFovDeg, 1.0, 0.01, 100);
    camera.up.set(0, 1, 0);

    const _w = window;
    const _origRandom = Math.random;

    let _seed = 0x12345678;
    Math.random = () => {
      _seed |= 0; _seed = _seed + 0x6D2B79F5 | 0;
      let t = Math.imul(_seed ^ _seed >>> 15, 1 | _seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };

    const PRE_IMPORT_TRAPPED = [
      'setTimeout', 'setInterval', 'setImmediate', 'queueMicrotask',
      'fetch', 'XMLHttpRequest', 'WebSocket',
      'document', 'navigator',
      'localStorage', 'sessionStorage', 'indexedDB',
      'OffscreenCanvas', 'HTMLCanvasElement',
      'crypto', 'Date', 'performance',
      'Proxy', 'Reflect',
      'WeakRef', 'FinalizationRegistry',
      'SharedArrayBuffer', 'Atomics',
      'Worker',
      'process', 'global', 'globalThis', 'self', 'require',
    ];
    const POST_IMPORT_TRAPPED = ['eval', 'Function', 'window'];
    const savedDescriptors = new Map();

    for (const name of PRE_IMPORT_TRAPPED) {
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

    const CODEGEN_PROTOS = [
      Function.prototype,
      Object.getPrototypeOf(function*(){}),
      Object.getPrototypeOf(async function(){}),
      Object.getPrototypeOf(async function*(){}),
    ];
    const savedCtors = [];
    for (const proto of CODEGEN_PROTOS) {
      const desc = Object.getOwnPropertyDescriptor(proto, 'constructor');
      savedCtors.push({ proto, desc });
      Object.defineProperty(proto, 'constructor', {
        get() { throw new Error('Runtime violation: Function constructor is forbidden'); },
        configurable: true,
      });
    }

    const blob = new Blob([source], { type: 'application/javascript' });
    const blobUrl = URL.createObjectURL(blob);
    let mod;
    try {
      mod = await import(blobUrl);
    } finally {
      URL.revokeObjectURL(blobUrl);
    }

    for (const name of POST_IMPORT_TRAPPED) {
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

    if (typeof mod.default !== 'function') {
      return { error: 'default export is not a function' };
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
      for (const { proto, desc } of savedCtors) {
        try { Object.defineProperty(proto, 'constructor', desc); }
        catch {}
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
        if (followLight) {
          followLight.position.copy(pos).add(new THREE.Vector3(1, 1, 0));
          followLight.lookAt(0, 0, 0);
        }

        renderer.render(scene, camera);

        const row = Math.floor(i / cols);
        const col = i % cols;
        ctx.drawImage(renderer.domElement, col * (params.imgSize + gap), row * (params.imgSize + gap));
      }

      renderer.dispose();
      return { data: gridCanvas.toDataURL('image/png') };
    }

    const images = [];
    for (let i = 0; i < params.thetas.length; i++) {
      const pos = sphericalToCartesian(params.thetas[i], params.phis[i], params.camRadius);
      camera.position.copy(pos);
      camera.lookAt(0, 0, 0);
      if (followLight) {
        followLight.position.copy(pos).add(new THREE.Vector3(1, 1, 0));
        followLight.lookAt(0, 0, 0);
      }

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

export const renderPool = new RenderPool();
