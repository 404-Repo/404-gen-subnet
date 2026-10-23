import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { availableParallelism } from 'node:os';
import { test } from 'node:test';
import { ensureBrowser, closeBrowser } from '../src/browser.js';
import { renderGrid } from '../src/renderer.js';
import { startStaticServer } from '../src/static-server.js';

// Run in the Linux Chromium image with a CPU quota below its visible CPU count.
const source = `export default function(THREE) {
  return new THREE.Mesh(new THREE.SphereGeometry(0.35, 24, 16),
    new THREE.MeshStandardMaterial({ color: 0xb0a060, metalness: 1, roughness: 0.3 }));
}`;
const options = { lighting: 'neutral', thetas: [30], phis: [0], imgSize: 128 };
const [quota, period] = readFileSync('/sys/fs/cgroup/cpu.max', 'utf8').trim().split(/\s+/);
const budget = quota === 'max' ? availableParallelism()
  : Math.min(availableParallelism(), Math.max(1, Math.floor(Number(quota) / Number(period))));

const server = await startStaticServer();
try {
  await test('software workers respect the CPU budget and browser replacement preserves PNG bytes', async () => {
    let expected;
    for (let launch = 0; launch < 2; launch++) {
      const browser = await ensureBrowser();
      for (let repeat = 0; repeat < 2; repeat++) {
        const png = await renderGrid(source, options);
        if (!expected) expected = png;
        else assert.ok(png.equals(expected), 'Cold, warm and replacement renders must match');
      }
      const taskRoot = `/proc/${browser.process().pid}/task`;
      const softwareWorkers = readdirSync(taskRoot).filter((tid) => {
        try { return /^Thread<\d+>\s*$/.test(readFileSync(`${taskRoot}/${tid}/comm`, 'utf8')); }
        catch (error) { if (error.code === 'ENOENT') return false; throw error; }
      });
      assert.ok(softwareWorkers.length > 0, 'SwiftShader must have active rendering workers');
      assert.ok(softwareWorkers.length <= budget,
        `${softwareWorkers.length} software workers exceed the ${budget}-CPU budget`);
      await closeBrowser();
    }
  });
} finally {
  await closeBrowser();
  await new Promise((resolve) => server.close(resolve));
}
