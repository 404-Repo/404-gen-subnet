/**
 * Cache the fixed lighting environments as half-float pixels, once per browser.
 * Baking runs in a trusted, disposable context without any miner code. Each
 * render uploads its own texture; no WebGL objects or miner contexts are reused.
 */
import { STATIC_PORT } from './static-server.js';

const environments = new WeakMap();
const PAGE_URL = `http://127.0.0.1:${STATIC_PORT}/render-page.html`;

export function getCachedPmrem(browser, lighting) {
  if (lighting === 'follow') return Promise.resolve(null);
  const mode = lighting === 'neutral' ? 'neutral' : 'studio';
  let entries = environments.get(browser);
  if (!entries) {
    entries = new Map();
    environments.set(browser, entries);
  }
  if (!entries.has(mode)) {
    const pending = bakeEnvironment(browser, mode).catch((error) => {
      entries.delete(mode);
      throw error;
    });
    entries.set(mode, pending);
  }
  return entries.get(mode);
}

async function bakeEnvironment(browser, lighting) {
  const context = await browser.createBrowserContext();
  try {
    const page = await context.newPage();
    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction('window.__ready === true', { timeout: 10_000 });
    await page.setOfflineMode(true);
    return await page.evaluate(bakeInPage, lighting);
  } finally {
    await context.close();
  }
}

async function bakeInPage(lighting) {
  const THREE = window.THREE;
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    preserveDrawingBuffer: true,
    alpha: true,
  });
  const pmrem = new THREE.PMREMGenerator(renderer);
  const envScene = new THREE.Scene();
  let target;
  try {
    if (lighting === 'neutral') {
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
      addPanel(4, 4, 2.6, 0, 4.9, 0, Math.PI / 2, 0);
      addPanel(3, 2, 1.4, -4.9, 1.5, 0, 0, Math.PI / 2);
      addPanel(3, 2, 1.1, 4.9, 1.0, -1, 0, -Math.PI / 2);
      addPanel(4, 1.5, 0.9, 0, 1.0, -4.9, 0, 0);
    } else {
      // Preserve the existing studio environment, including its black cubemap.
      envScene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.0));
    }

    target = pmrem.fromScene(envScene, 0.04);
    const gl = renderer.getContext();
    if (gl.isContextLost() || gl.getError() !== gl.NO_ERROR) {
      throw new Error('PMREM baking failed before pixel readback');
    }
    const pixels = new Uint16Array(target.width * target.height * 4);
    // The synchronous API can silently skip a read. Require a completed transfer
    // and a healthy context before publishing these pixels to the shared cache.
    const readback = await renderer.readRenderTargetPixelsAsync(
      target, 0, 0, target.width, target.height, pixels,
    );
    if (readback !== pixels || gl.isContextLost() || gl.getError() !== gl.NO_ERROR) {
      throw new Error('PMREM pixel readback failed');
    }
    const bytes = new Uint8Array(pixels.buffer);
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += 32768) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
    }
    return { width: target.width, height: target.height, data: btoa(binary) };
  } finally {
    target?.dispose();
    pmrem.dispose();
    envScene.traverse((object) => {
      object.geometry?.dispose();
      object.material?.dispose();
    });
    renderer.dispose();
  }
}
