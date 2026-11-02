import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import puppeteer from 'puppeteer';
import { ensureBrowser, closeBrowser } from '../src/browser.js';

if (process.argv[2] === '--serve') {
  let fault;
  let kills = 0;
  async function hit(stage, browser) {
    if (fault?.stage !== stage || fault.remaining === 0) return;
    fault.remaining--;
    if (fault.delay) await new Promise((resolve) => setTimeout(resolve, fault.delay));
    if (stage !== 'stall') {
      const exited = once(browser.process(), 'exit');
      browser.process().kill('SIGKILL');
      await exited;
      kills++;
    }
  }
  const launch = puppeteer.launch.bind(puppeteer);
  puppeteer.launch = async (...args) => {
    const browser = await launch(...args);
    const createContext = browser.createBrowserContext.bind(browser);
    browser.createBrowserContext = async (...args) => {
      await hit('context', browser);
      await hit('stall', browser);
      const context = await createContext(...args);
      const newPage = context.newPage.bind(context);
      context.newPage = async () => {
        const page = await newPage();
        const evaluate = page.evaluate.bind(page);
        page.evaluate = async (fn, ...args) => {
          if (fn.name === 'renderInPage') await hit('render', browser);
          return evaluate(fn, ...args);
        };
        return page;
      };
      return context;
    };
    return browser;
  };
  process.on('message', async (value) => {
    if (value.fault) {
      fault = { stage: value.fault, remaining: value.count ?? 1, delay: value.delay ?? 0 };
      kills = 0;
      process.send({ armed: true });
    } else {
      const browser = await ensureBrowser();
      process.send({ kills, contexts: browser.browserContexts().length });
    }
  });
  process.once('SIGTERM', async () => { await closeBrowser(); process.exit(0); });
  const { main } = await import('../src/server.js');
  await main();
} else {
  const source = `export default function(THREE) {
    return new THREE.Mesh(new THREE.SphereGeometry(0.35, 24, 16),
      new THREE.MeshStandardMaterial({ color: 0xb0a060, metalness: 1, roughness: 0.3 }));
  }`;
  async function serve(timeout, scenario) {
    const child = spawn(process.execPath, [fileURLToPath(import.meta.url), '--serve'], {
      env: { ...process.env, PORT: '18082', RENDER_POOL_SIZE: '1', VALIDATION_POOL_SIZE: '1', RENDER_TIMEOUT_MS: String(timeout) },
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
    });
    const exited = once(child, 'exit');
    let logs = '';
    child.stdout.on('data', (chunk) => { logs = (logs + chunk).slice(-16000); });
    child.stderr.on('data', (chunk) => { logs = (logs + chunk).slice(-16000); });
    const post = (path) => fetch(`http://127.0.0.1:18082${path}?lighting=neutral&thetas=30&phis=0&img_size=128&gap=0`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source }), signal: AbortSignal.timeout(35_000),
    });
    const command = async (value) => {
      const reply = once(child, 'message');
      child.send(value);
      return (await reply)[0];
    };
    try {
      const deadline = Date.now() + 30_000;
      let ready = false;
      while (Date.now() < deadline && child.exitCode === null && child.signalCode === null) {
        try {
          if ((await fetch('http://127.0.0.1:18082/ping', { signal: AbortSignal.timeout(1000) })).status === 200) {
            ready = true; break;
          }
        } catch {}
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      assert.ok(ready, logs);
      await scenario(post, command);
    } catch (error) {
      console.error(logs);
      throw error;
    } finally {
      const forceKill = setTimeout(() => child.kill('SIGKILL'), 5000);
      try { child.kill('SIGTERM'); await exited; }
      finally { clearTimeout(forceKill); }
    }
  }
  await serve(30_000, async (post, command) => {
    const expected = {};
    for (const path of ['/render', '/render/grid']) {
      const response = await post(path);
      assert.equal(response.status, 200, await response.clone().text());
      expected[path] = Buffer.from(await response.arrayBuffer());
    }
    for (const [path, fault] of [['/render', 'context'], ['/render/grid', 'render']]) {
      await test(`${path}: SIGKILL at ${fault} recovers the same request and queued work`, async (t) => {
        await command({ fault });
        const started = performance.now();
        const responses = await Promise.all(Array.from({ length: 3 }, () => post(path)));
        for (const response of responses) {
          assert.equal(response.status, 200, await response.clone().text());
          assert.ok(Buffer.from(await response.arrayBuffer()).equals(expected[path]), 'PNG bytes changed');
        }
        assert.equal((await command({})).kills, 1);
        t.diagnostic(`Three requests completed in ${(performance.now() - started).toFixed(0)}ms`);
      });
    }
    await test('two browser deaths stop after one replacement attempt', async () => {
      await command({ fault: 'context', count: 2 });
      const response = await post('/render');
      assert.equal(response.status, 500);
      await response.text();
      assert.equal((await command({})).kills, 2);
      const next = await post('/render');
      assert.equal(next.status, 200, await next.clone().text());
      assert.ok(Buffer.from(await next.arrayBuffer()).equals(expected['/render']));
    });
  });
  await serve(1000, async (post, command) => {
    await test('context setup consumes the rendering deadline and late contexts are closed', async () => {
      await command({ fault: 'stall', delay: 1500 });
      const started = performance.now();
      const response = await post('/render');
      assert.equal(response.status, 500);
      assert.deepEqual(await response.json(), { error: 'render timed out after 1000ms' });
      assert.ok(performance.now() - started < 1500, 'setup extended the deadline');
      await new Promise((resolve) => setTimeout(resolve, 1000));
      assert.deepEqual(await command({}), { kills: 0, contexts: 1 });
    });
  });

  process.env.RENDER_TIMEOUT_MS = '1000';
  const { renderPool: launchPool } = await import('../src/render-pool.js');
  await test('timed-out requests share a late launch and shutdown closes every browser', async () => {
    const launch = puppeteer.launch;
    const pending = [];
    const browsers = [];
    let delay = 0;
    let maxLive = 0;
    puppeteer.launch = (...args) => {
      const started = (async () => {
        const browser = await launch.apply(puppeteer, args);
        browsers.push(browser);
        maxLive = Math.max(maxLive, browsers.filter((b) =>
          b.process().exitCode === null && b.process().signalCode === null).length);
        if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
        return browser;
      })();
      pending.push(started);
      return started;
    };
    try {
      const initial = await ensureBrowser();
      const exited = once(initial.process(), 'exit');
      initial.process().kill('SIGKILL');
      await exited;
      delay = 4000;
      for (let request = 0; request < 2; request++) {
        await assert.rejects(launchPool.renderGrid('', { lighting: 'follow' }),
          /render timed out after 1000ms/);
      }
      const closed = closeBrowser();
      await Promise.all(pending);
      await closed;
      assert.equal(maxLive, 1, 'Late launches created overlapping Chromium processes');
      assert.ok(browsers.every((b) => b.process().exitCode !== null || b.process().signalCode !== null),
        'Shutdown left a Chromium process alive');
    } finally {
      await Promise.allSettled(pending);
      await Promise.allSettled(browsers.map((b) => b.close()));
      await closeBrowser();
      puppeteer.launch = launch;
    }
  });

  process.env.RENDER_TIMEOUT_MS = '10000';
  const { renderPool: cleanupPool } = await import('../src/render-pool.js?cleanup');
  const { startStaticServer } = await import('../src/static-server.js');
  const staticServer = await startStaticServer();
  const params = { lighting: 'follow', thetas: [30], phis: [0], imgSize: 128, gap: 0 };
  try {
    for (const stage of ['completed render', 'stalled render']) {
      await test(`${stage}: stalled cleanup stays within the deadline and queued work recovers`, async (t) => {
        const browser = await ensureBrowser();
        const expected = await cleanupPool.renderGrid(source, params);
        const createContext = browser.createBrowserContext.bind(browser);
        let release;
        const held = new Promise((resolve) => { release = resolve; });
        browser.createBrowserContext = async (...args) => {
          browser.createBrowserContext = createContext;
          const context = await createContext(...args);
          const close = context.close.bind(context);
          context.close = async () => { await held; await close(); };
          return context;
        };
        const started = performance.now();
        const first = cleanupPool.renderGrid(stage === 'stalled render'
          ? 'export default function(THREE) { while (true) {} }' : source, params)
          .then((png) => ({ png }), (error) => ({ error }));
        const queued = cleanupPool.renderGrid(source, params)
          .then((png) => ({ png }), (error) => ({ error }));
        try {
          const result = await Promise.race([first, new Promise((resolve) => {
            setTimeout(() => resolve({ overdue: true }), 11500).unref();
          })]);
          assert.ok(!result.overdue, 'Context cleanup extended the rendering deadline');
          assert.match(result.error?.message ?? '', /render timed out after 10000ms/);
          t.diagnostic(`Failed request completed in ${(performance.now() - started).toFixed(0)}ms`);
          const next = await queued;
          assert.ok(next.png?.equals(expected), next.error?.message ?? 'Queued PNG bytes changed');
          assert.equal(browser.process().signalCode, 'SIGKILL', 'The stalled browser was not terminated');
        } finally {
          release();
          await Promise.allSettled([first, queued]);
          browser.createBrowserContext = createContext;
          await closeBrowser();
        }
      });
    }
  } finally {
    await closeBrowser();
    await new Promise((resolve) => staticServer.close(resolve));
  }
}
