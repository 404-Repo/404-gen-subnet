/** Exercise real HTTP validation and render deadlines with delayed browser I/O. */
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import puppeteer from 'puppeteer';

process.env.RENDER_TIMEOUT_MS = '40';
process.env.FINAL_RENDER_TIMEOUT_MS = '200';
process.env.VALIDATION_POOL_SIZE = '1';

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN3sAAAAASUVORK5CYII=', 'base64');
const DATA_URL = `data:image/png;base64,${PNG.toString('base64')}`;
const SOURCE = `export default function generate(THREE) {
  return new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5), new THREE.MeshStandardMaterial());
}`;
let renderMs = 80;

const launch = puppeteer.launch;
puppeteer.launch = async () => {
  const browser = {
    connected: true,
    async close() { this.connected = false; },
    disconnect() { this.connected = false; },
    async createBrowserContext() {
      return {
        async close() {},
        async newPage() {
          return {
            on() {},
            async goto() {},
            async waitForFunction() {},
            async setOfflineMode() {},
            async evaluate(fn, source, params, mode) {
              await delay(renderMs);
              return { data: mode === 'views' ? params.thetas.map(() => DATA_URL) : DATA_URL };
            },
          };
        },
      };
    },
  };
  return browser;
};

const { server } = await import('../src/server.js');
const { validationPool } = await import('../src/validation-pool.js');
const { closeBrowser } = await import('../src/browser.js');
await validationPool.init();
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
after(async () => {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  await validationPool.shutdown();
  await closeBrowser();
  puppeteer.launch = launch;
});

async function post(route, purpose) {
  return fetch(base + route + '?lighting=follow&thetas=30,330,0,0&phis=0,0,15,-30&img_size=8', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(purpose ? { 'X-Render-Purpose': purpose } : {}) },
    body: JSON.stringify({ source: SOURCE }),
    signal: AbortSignal.timeout(5000),
  });
}

for (const route of ['/render', '/render/grid']) {
  test(`${route}: a final batch can exceed the preview deadline`, async () => {
    renderMs = 80;
    const final = await post(route, 'final');
    assert.equal(final.status, 200, await final.clone().text());
    if (route === '/render') {
      assert.deepEqual((await final.json()).images, Array(4).fill(PNG.toString('base64')));
    } else {
      assert.deepEqual(Buffer.from(await final.arrayBuffer()), PNG);
    }

    const preview = await post(route);
    assert.equal(preview.status, 504);
    assert.deepEqual(await preview.json(), { error: 'render_timeout', timeout_ms: 40 });
  });

  test(`${route}: the final deadline remains bounded and the next request recovers`, async () => {
    renderMs = 300;
    const response = await post(route, 'final');
    assert.equal(response.status, 504);
    assert.deepEqual(await response.json(), { error: 'render_timeout', timeout_ms: 200 });

    renderMs = 0;
    const next = await post(route);
    assert.equal(next.status, 200, await next.clone().text());
    await next.arrayBuffer();
  });
}
