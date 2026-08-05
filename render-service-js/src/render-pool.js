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
import { SAFE_THREE_BROWSER_BUNDLE } from './safeThree.js';
import {
  THREE_ALLOWED,
  THREE_BLOCKED_SUBMEMBERS,
  THREE_DISALLOWED_KNOWN,
} from './validator/threeAllowlist.js';

// Config shipped to the Puppeteer page context so renderInPage can
// reconstruct the safeTHREE Proxy inside the browser. Plain JSON-safe
// shapes (arrays / plain objects) — Puppeteer serializes evaluate args.
const SAFE_THREE_CONFIG = {
  allowed: [...THREE_ALLOWED],
  disallowedKnown: [...THREE_DISALLOWED_KNOWN],
  blockedSubmembers: Object.fromEntries(
    Object.entries(THREE_BLOCKED_SUBMEMBERS).map(([k, v]) => [k, [...v]]),
  ),
};

const RENDER_TIMEOUT_MS = parseInt(process.env.RENDER_TIMEOUT_MS || '30000', 10);
const POOL_SIZE = parseInt(process.env.RENDER_POOL_SIZE || '1', 10);
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
        page.evaluate(
          renderInPage,
          source,
          params,
          mode,
          SAFE_THREE_CONFIG,
          SAFE_THREE_BROWSER_BUNDLE,
        ),
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
 *
 * Extra args:
 *   - safeThreeConfig: plain-JSON allowlist / disallowed-known /
 *     blocked-submember data, serializable across the Puppeteer bridge.
 *   - safeThreeBundle: the `SAFE_THREE_BROWSER_BUNDLE` string from
 *     src/safeThree.js. The bundle is the Function.prototype.toString()
 *     source of the same builders `createSafeThree` calls on the Node
 *     side, so the two execution environments share one implementation
 *     and cannot drift.
 *
 * The Proxy is constructed BEFORE the page's global `Proxy` and `Reflect`
 * are trapped below — once those are trapped, `new Proxy(...)` itself
 * would throw.
 */
async function renderInPage(source, params, mode, safeThreeConfig, safeThreeBundle) {
  try {
    const THREE = window.THREE;

    // Reconstruct `buildSafeThreeWithConfig` from the bundle string and
    // invoke it. The bundle was produced by `BUILDERS.map(fn => fn.toString())`
    // on the Node side; splicing it into a `new Function` body recreates
    // byte-identical builders inside the browser realm. Hoisting makes
    // the trailing `return buildSafeThreeWithConfig(...)` resolvable
    // regardless of builder order inside the bundle.
    //
    // This step must run BEFORE the global trapping below, because the
    // traps shadow `Proxy`, `Reflect`, `eval`, and `Function` — all of
    // which `new Function` implicitly uses.
    const SAFE_THREE = new Function(
      'THREE', 'cfg',
      `${safeThreeBundle}\nreturn buildSafeThreeWithConfig(THREE, cfg);`,
    )(THREE, safeThreeConfig);

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
    } else if (params.lighting === 'neutral') {
      // Camera-follow key light (view-stable shading, as in 'follow') plus a
      // real luminous environment. 'studio' builds its envmap from a scene
      // containing only a HemisphereLight — lights are not visible geometry,
      // so that cubemap is black and metalness>=0.9 materials render as black
      // mirrors. Here the PMREM scene contains actual emissive surfaces
      // (RoomEnvironment-style shell + panels), so metals reflect something
      // and dielectrics pick up soft ambient, at levels calibrated to keep
      // non-metal luminance close to the 'follow' baseline.
      // Exposure is metered, not eyeballed: a photographic 18% grey card (a
      // matte sphere at 0.18 linear reflectance) must land near 118 sRGB —
      // middle grey — so neither dark nor pale submissions are systematically
      // mis-scored. Scoped to this branch so `follow` and `studio` output
      // stays bit-for-bit what it was.
      renderer.toneMappingExposure = 1.32;

      followLight = new THREE.DirectionalLight(0xffffff, 1.1);
      scene.add(followLight);

      const pmrem = new THREE.PMREMGenerator(renderer);
      const envScene = new THREE.Scene();

      const shellMat = new THREE.MeshBasicMaterial({ side: THREE.BackSide });
      shellMat.color.setRGB(0.32, 0.32, 0.32);
      envScene.add(new THREE.Mesh(new THREE.BoxGeometry(10, 10, 10), shellMat));

      const addPanel = (w, h, intensity, x, y, z, rx, ry) => {
        const mat = new THREE.MeshBasicMaterial();
        mat.color.setRGB(intensity, intensity, intensity);
        const panel = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat);
        panel.position.set(x, y, z);
        panel.rotation.set(rx, ry, 0);
        envScene.add(panel);
      };
      addPanel(4, 4, 2.6, 0, 4.9, 0, Math.PI / 2, 0);     // ceiling key panel
      addPanel(3, 2, 1.4, -4.9, 1.5, 0, 0, Math.PI / 2);  // left fill panel
      addPanel(3, 2, 1.1, 4.9, 1.0, -1, 0, -Math.PI / 2); // right fill panel
      addPanel(4, 1.5, 0.9, 0, 1.0, -4.9, 0, 0);          // back rim panel

      scene.environment = pmrem.fromScene(envScene, 0.04).texture;
      scene.environmentIntensity = 0.9;
      pmrem.dispose();
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
      // Capability-filtered THREE: every `X.Foo` access inside miner code
      // flows through the Proxy's get trap and is either allowed or thrown.
      root = mod.default(SAFE_THREE);
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

    // Lighting is renderer-controlled, so a material must not be able to opt
    // out of it. Three.js skips `scene.environment` for any material that sets
    // its own `envMap` (WebGLRenderer only forwards `scene.environmentIntensity`
    // when `material.envMap === null`), and a raw non-PMREM texture is not
    // valid IBL input — so assigning one silently disables image-based
    // lighting and returns metals to the black-mirror look this mode fixes.
    // Clearing it keeps every material on the renderer's environment.
    // Three.js invokes these hooks with the live renderer, scene, and camera
    // as arguments — a reference the safeTHREE Proxy cannot filter, since it
    // is handed in rather than reached through the THREE namespace. A scene
    // that sets one can rewrite `toneMappingExposure` (or any other renderer
    // state) mid-frame. Nothing in the object model needs them for a static
    // single-frame render, so drop them.
    root.traverse((obj) => {
      if (obj.onBeforeRender) obj.onBeforeRender = () => {};
      if (obj.onAfterRender) obj.onAfterRender = () => {};

      const materials = Array.isArray(obj.material) ? obj.material : [obj.material];
      for (const material of materials) {
        if (material && material.envMap) {
          material.envMap = null;
          material.needsUpdate = true;
        }
      }
    });

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
