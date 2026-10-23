/**
 * Compare complete PNG bytes against an unmodified renderer in the same runtime.
 * PMREM_REFERENCE_DIR points to render-service-js from the pre-cache revision,
 * placed below /app so both copies resolve the same locked node_modules.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { startStaticServer } from '../src/static-server.js';
import { renderViews, renderGrid } from '../src/renderer.js';
import { ensureBrowser, closeBrowser } from '../src/browser.js';

const referenceDir = process.env.PMREM_REFERENCE_DIR;
assert.ok(referenceDir, 'Set PMREM_REFERENCE_DIR to the unmodified renderer directory');
const reference = await import(pathToFileURL(path.join(referenceDir, 'src/renderer.js')));
const referenceBrowser = await import(pathToFileURL(path.join(referenceDir, 'src/browser.js')));
const fixtureDir = process.env.PMREM_FIXTURES || '/fixtures';
const output = process.env.PMREM_TEST_OUTPUT;
const hash = (data) => crypto.createHash('sha256').update(data).digest('hex');

const fixtures = [
  'car', 'custom_buffergeometry', 'instanced_mesh', 'line_segments',
  'material_variants', 'points_cloud', 'generated/balloon', 'generated/clock',
  'generated/pumpkin', 'generated/suv',
].map((name) => ({ name, source: fs.readFileSync(path.join(fixtureDir, `${name}.js`), 'utf8') }));
fixtures.push({ name: 'roughness-sweep', source: `
export default function(THREE) {
  const root = new THREE.Group();
  for (let i = 0; i < 6; i++) {
    for (let row = 0; row < 2; row++) {
      const settings = { color: 0xb0a060, metalness: 1, roughness: i / 5 };
      const material = row === 0 ? new THREE.MeshStandardMaterial(settings)
        : new THREE.MeshPhysicalMaterial({ ...settings, clearcoat: 1, clearcoatRoughness: i / 5 });
      const mesh = new THREE.Mesh(new THREE.SphereGeometry(0.12, 24, 16), material);
      mesh.position.set((i - 2.5) * 0.3, (row - 0.5) * 0.4, 0);
      root.add(mesh);
    }
  }
  return root;
}` });

const profiles = [
  { name: 'neutral-white-8x1024', mode: 'views', options: {
    thetas: [30, 330, 0, 0, 90, 180, 270, 0], phis: [0, 0, 15, -30, 0, 0, 0, -90],
    imgSize: 1024, bgColor: 'ffffff', lighting: 'neutral',
  } },
  { name: 'neutral-gray-4x1024', mode: 'views', options: {
    thetas: [30, 330, 0, 0], phis: [0, 0, 15, -30],
    imgSize: 1024, bgColor: '808080', lighting: 'neutral',
  } },
  { name: 'studio-grid-518', mode: 'grid', options: {} },
];
const cases = fixtures.flatMap((fixture) => profiles.map((profile) => ({ ...fixture, ...profile,
  name: `${fixture.name.replaceAll('/', '-')}-${profile.name}`,
})));
cases.push({ name: 'follow-control', source: fixtures[0].source, mode: 'grid', options: {
  lighting: 'follow', thetas: [0, 90, 180, 270], phis: [0, 15, -15, 30], imgSize: 256,
  bgColor: 'ffffff', gap: 0,
} });

const report = { image: process.env.PMREM_IMAGE_ID || null, reference: hash(
  fs.readFileSync(path.join(referenceDir, 'src/render-pool.js')),
), records: [] };
const save = () => {
  if (output) fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
};
if (output) fs.mkdirSync(output, { recursive: true });

async function render(api, item, stage) {
  const start = performance.now();
  let images;
  try {
    images = item.mode === 'grid' ? [await api.renderGrid(item.source, item.options)]
      : await api.renderViews(item.source, item.options);
  } catch (cause) {
    throw new Error(`${stage}: ${item.name}: ${cause?.message || String(cause)}`, { cause });
  }
  const record = { name: item.name, stage, milliseconds: Math.round(performance.now() - start),
    pngs: images.map(hash), source: hash(item.source), options: item.options };
  report.records.push(record);
  console.log(JSON.stringify(record));
  if (output) images.forEach((png, i) => fs.writeFileSync(path.join(output, `${stage}-${item.name}-${i}.png`), png));
  save();
  return images;
}

const server = await startStaticServer();
const expected = new Map();
let comparisons = 0;
function compare(item, actual) {
  const baseline = expected.get(item.name);
  assert.equal(actual.length, baseline.length, `${item.name}: image count`);
  actual.forEach((png, index) => {
    assert.ok(png.equals(baseline[index]), `${item.name}[${index}]: PNG bytes changed`);
    comparisons++;
  });
}
try {
  for (const item of cases) expected.set(item.name, await render(reference, item, 'reference'));
  await referenceBrowser.closeBrowser();
  const api = { renderViews, renderGrid };
  for (const item of cases) compare(item, await render(api, item, 'cached'));

  await assert.rejects(renderViews(`export default function(THREE) {
    throw new Error('intentional PMREM test failure');
  }`, { lighting: 'neutral', imgSize: 128 }), /intentional PMREM test failure/);
  compare(cases[1], await render(api, cases[1], 'after-failure'));

  // Warm reuse across request parameters and fresh miner contexts.
  for (const item of [cases[0], cases[2], cases.at(-1)]) {
    compare(item, await render(api, item, 'repeat'));
  }
  await closeBrowser();
  for (const item of [cases[0], cases[2]]) {
    compare(item, await render(api, item, 'browser-restart'));
  }
  // A fresh browser plus simultaneous requests exercises cache-miss coalescing.
  await closeBrowser();
  // Match service startup: initialize Chromium before accepting render requests.
  const browser = await ensureBrowser();
  const createContext = browser.createBrowserContext;
  let contexts = 0;
  browser.createBrowserContext = function (...args) {
    contexts++;
    return createContext.apply(this, args);
  };
  try {
    await Promise.all([cases[0], cases[1]].map(async (item) => {
      compare(item, await render(api, item, 'concurrent'));
    }));
    assert.equal(contexts, 3, 'Two miner contexts must share one PMREM baking context');
  } finally {
    browser.createBrowserContext = createContext;
  }
  report.browser = await browser.version();
  report.complete = true;
  report.comparisons = comparisons;
  report.concurrentContexts = contexts;
  save();
  console.log(`PASS: ${comparisons} PNGs byte-identical to the unmodified renderer`);
} finally {
  await referenceBrowser.closeBrowser();
  await closeBrowser();
  await new Promise((resolve) => server.close(resolve));
}
