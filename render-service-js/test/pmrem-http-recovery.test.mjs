import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { ensureBrowser, closeBrowser } from '../src/browser.js';
import { renderViews, renderGrid } from '../src/renderer.js';
import { startStaticServer } from '../src/static-server.js';

if (process.argv[2] === '--serve') {
  // Inject a real context loss during the first async GPU wait for each mode.
  // Three.js rejects that wait without a reason; no HTTP behavior is mocked.
  const browser = await ensureBrowser();
  const createContext = browser.createBrowserContext;
  const injected = new Set();
  browser.createBrowserContext = async function (...args) {
    const context = await createContext.apply(this, args);
    const newPage = context.newPage.bind(context);
    context.newPage = async () => {
      const page = await newPage();
      const evaluate = page.evaluate.bind(page);
      page.evaluate = async (fn, ...params) => {
        if (fn.name === 'bakeInPage' && !injected.has(params[0])) {
          injected.add(params[0]);
          await evaluate(() => {
            const original = WebGL2RenderingContext.prototype.clientWaitSync;
            WebGL2RenderingContext.prototype.clientWaitSync = function (...args) {
              this.getExtension('WEBGL_lose_context').loseContext();
              return original.apply(this, args);
            };
          });
        }
        try {
          return await evaluate(fn, ...params);
        } catch (error) {
          console.error(`[http-test] ${fn.name} failed: ${String(error)}`);
          throw error;
        }
      };
      return page;
    };
    return context;
  };
  process.once('SIGTERM', async () => {
    await closeBrowser();
    process.exit(0);
  });
  await import('../src/server.js');
} else {
  const source = `export default function(THREE) {
    return new THREE.Mesh(new THREE.SphereGeometry(0.35, 24, 16),
      new THREE.MeshStandardMaterial({ color: 0xb0a060, metalness: 1, roughness: 0.3 }));
  }`;
  const options = { thetas: [30], phis: [0], imgSize: 128, gap: 0, bgColor: '808080' };
  const cases = [
    { path: '/render', lighting: 'neutral' },
    { path: '/render/grid', lighting: 'studio' },
  ];
  const staticServer = await startStaticServer();
  try {
    await ensureBrowser();
    cases[0].expected = (await renderViews(source, { ...options, lighting: 'neutral' }))[0];
    cases[1].expected = await renderGrid(source, { ...options, lighting: 'studio' });
  } finally {
    await closeBrowser();
    await new Promise((resolve) => staticServer.close(resolve));
  }

  const base = 'http://127.0.0.1:18081';
  const child = spawn(process.execPath, [fileURLToPath(import.meta.url), '--serve'], {
    // Leave headroom for cold software rendering under CPU quotas.
    env: { ...process.env, PORT: '18081', VALIDATION_POOL_SIZE: '1', RENDER_TIMEOUT_MS: '60000' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const exited = once(child, 'exit');
  let logs = '';
  child.stdout.on('data', (chunk) => { logs = (logs + chunk).slice(-16000); });
  child.stderr.on('data', (chunk) => { logs = (logs + chunk).slice(-16000); });
  const get = (path) => fetch(`${base}${path}`, { signal: AbortSignal.timeout(2000) });
  const post = (item) => fetch(`${base}${item.path}?lighting=${item.lighting}&thetas=30&phis=0&img_size=128&gap=0&bg_color=808080`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source }), signal: AbortSignal.timeout(75_000),
  });
  try {
    const deadline = Date.now() + 30_000;
    let ready = false;
    while (Date.now() < deadline && child.exitCode === null && child.signalCode === null) {
      try {
        if ((await get('/ping')).status === 200) { ready = true; break; }
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    assert.ok(ready, `HTTP server did not become ready:\n${logs}`);

    for (const item of cases) {
      await test(`${item.path}: a failed GPU wait returns 500 and recovers without restarting`, async (t) => {
        try {
          const failed = await post(item);
          assert.equal(failed.status, 500);
          assert.deepEqual(await failed.json(), { error: 'PMREM pixel readback failed' });
          assert.equal(child.exitCode, null);
          assert.equal(child.signalCode, null);
          assert.equal((await get('/ping')).status, 200);
          assert.equal((await get('/health')).status, 200);

          for (const stage of ['rebuilt', 'cached']) {
            const response = await post(item);
            assert.equal(response.status, 200,
              response.status === 200 ? stage : `${stage}: ${await response.text()}`);
            assert.match(response.headers.get('content-type'), /image\/png/);
            const actual = Buffer.from(await response.arrayBuffer());
            assert.ok(actual.equals(item.expected), `${stage}: PNG bytes changed`);
          }
        } catch (error) {
          t.diagnostic(logs);
          throw error;
        }
      });
    }
  } finally {
    const forceKill = setTimeout(() => child.kill('SIGKILL'), 5000);
    try {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
      await exited;
    } finally {
      clearTimeout(forceKill);
    }
  }
}
