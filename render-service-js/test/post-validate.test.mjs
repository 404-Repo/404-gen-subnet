/**
 * Regression tests for post-execution validation.
 *
 * Tests the postValidate() function directly using Three.js in Node.js,
 * and the validation worker pool for end-to-end execution + post-validation.
 *
 * Run:  node test/post-validate.test.mjs
 *       npm run test:postval
 */

import * as THREE from 'three';
import { postValidate } from '../src/post-validate.js';
import { staticValidate } from '../src/validator/index.js';
import { validationPool } from '../src/validation-pool.js';

function toPoolSource(esm) {
  const r = staticValidate(esm);
  if (r.passed) return r.transformed;
  return esm.replace(/\bexport\s+default\s+/, 'return ');
}
const poolValidate = (esm) => validationPool.validate(toPoolSource(esm));

let passed = 0;
let failed = 0;

function assert(name, condition, detail = '') {
  if (condition) {
    passed++;
  } else {
    failed++;
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

function assertRule(name, result, expectedRule) {
  if (!result.passed && result.failures.some(f => f.rule === expectedRule)) {
    passed++;
  } else if (result.passed) {
    failed++;
    console.error(`FAIL ${name} — expected ${expectedRule}, got: pass (metrics: ${JSON.stringify(result.metrics)})`);
  } else {
    failed++;
    console.error(`FAIL ${name} — expected ${expectedRule}, got: ${result.failures.map(f => f.rule).join(', ')}`);
  }
}

console.log('=== postValidate unit tests ===\n');

{
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(0.5, 0.5, 0.5),
    new THREE.MeshStandardMaterial({ color: 0x2194ce }),
  );
  const result = postValidate(THREE, mesh);
  assert('valid mesh passes', result.passed);
  assert('valid mesh — vertices > 0', result.metrics.vertices > 0);
  assert('valid mesh — drawCalls == 1', result.metrics.drawCalls === 1);
  assert('valid mesh — bbox within bounds', result.metrics.bbox != null);
}

{
  const group = new THREE.Group();
  for (let i = 0; i < 5; i++) {
    const m = new THREE.Mesh(
      new THREE.BoxGeometry(0.08, 0.08, 0.08),
      new THREE.MeshStandardMaterial(),
    );
    m.position.set((i - 2) * 0.15, 0, 0);
    group.add(m);
  }
  const result = postValidate(THREE, group);
  assert('valid group passes', result.passed);
  assert('valid group — drawCalls == 5', result.metrics.drawCalls === 5);
}

{
  const result = postValidate(THREE, null);
  assertRule('null root rejected', result, 'INVALID_RETURN_TYPE');
}

{
  const result = postValidate(THREE, undefined);
  assertRule('undefined root rejected', result, 'INVALID_RETURN_TYPE');
}

{
  const result = postValidate(THREE, { notAnObject3D: true });
  assertRule('non-Object3D rejected', result, 'INVALID_RETURN_TYPE');
}

{
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(3, 3, 3),
    new THREE.MeshStandardMaterial(),
  );
  const result = postValidate(THREE, mesh);
  assertRule('oversized bbox rejected', result, 'BOUNDING_BOX_OUT_OF_RANGE');
}

{
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(0.1, 0.1, 0.1),
    new THREE.MeshStandardMaterial(),
  );
  mesh.position.set(5, 0, 0);
  const result = postValidate(THREE, mesh);
  assertRule('offset bbox rejected', result, 'BOUNDING_BOX_OUT_OF_RANGE');
}

{
  const group = new THREE.Group();
  const result = postValidate(THREE, group);
  assertRule('empty scene rejected', result, 'EMPTY_SCENE');
}

{
  let parent = new THREE.Group();
  const root = parent;
  for (let i = 0; i < 35; i++) {
    const child = new THREE.Group();
    parent.add(child);
    parent = child;
  }
  parent.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), new THREE.MeshStandardMaterial()));
  const result = postValidate(THREE, root);
  assertRule('deep nesting rejected', result, 'DEPTH_LIMIT_EXCEEDED');
}

{
  const group = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial();
  for (let i = 0; i < 210; i++) {
    group.add(new THREE.Mesh(new THREE.BoxGeometry(0.01, 0.01, 0.01), mat));
  }
  const result = postValidate(THREE, group);
  assertRule('too many draw calls rejected', result, 'DRAW_CALL_LIMIT_EXCEEDED');
}

{
  const geo = new THREE.BoxGeometry(0.05, 0.05, 0.05);
  const mat = new THREE.MeshStandardMaterial();
  const im = new THREE.InstancedMesh(geo, mat, 60000);
  const dummy = new THREE.Object3D();
  for (let i = 0; i < 100; i++) {
    dummy.position.set(0, 0, 0);
    dummy.updateMatrix();
    im.setMatrixAt(i, dummy.matrix);
  }
  im.count = 60000;
  const result = postValidate(THREE, im);
  assertRule('too many instances rejected', result, 'INSTANCE_LIMIT_EXCEEDED');
}

{
  const dataSize = 1200000;
  const data = new Uint8Array(dataSize);
  const tex = new THREE.DataTexture(data, 1000, 300, THREE.RGBAFormat);
  const mat = new THREE.MeshStandardMaterial({ map: tex });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(0.5, 0.5), mat);
  const result = postValidate(THREE, mesh);
  assertRule('texture data exceeded rejected', result, 'TEXTURE_DATA_EXCEEDED');
}

{
  const a = new THREE.Group();
  const b = new THREE.Group();
  a.add(b);
  b.children.push(a);
  a.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), new THREE.MeshStandardMaterial()));
  const result = postValidate(THREE, a);
  assertRule('cycle detected', result, 'CYCLE_DETECTED');
}

{
  const geo = new THREE.BoxGeometry(0.3, 0.3, 0.3);
  const mat = new THREE.MeshStandardMaterial();
  const im = new THREE.InstancedMesh(geo, mat, 100);
  const dummy = new THREE.Object3D();
  for (let i = 0; i < 100; i++) {
    dummy.position.set(0, 0, 0);
    dummy.updateMatrix();
    im.setMatrixAt(i, dummy.matrix);
  }
  const result = postValidate(THREE, im);
  assert('instanced mesh passes', result.passed);
  assert('instanced mesh — instances == 100', result.metrics.instances === 100);
  assert('instanced mesh — drawCalls == 1', result.metrics.drawCalls === 1);
}

{
  const data = new Uint8Array(4 * 4 * 4);
  const tex = new THREE.DataTexture(data, 4, 4, THREE.RGBAFormat);
  const mat = new THREE.MeshStandardMaterial({ map: tex });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(0.5, 0.5), mat);
  const result = postValidate(THREE, mesh);
  assert('small texture passes', result.passed);
  assert('small texture — textureBytes == 64', result.metrics.textureBytes === 64);
}

{
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(0.5, 0.5, 0.5),
    new THREE.MeshStandardMaterial(),
  );
  const result = postValidate(THREE, mesh);
  assert('auto-wrap mesh in group', result.passed);
  assert('auto-wrap — drawCalls == 1', result.metrics.drawCalls === 1);
}

{
  const mesh = new THREE.Mesh(
    new THREE.SphereGeometry(0.4, 500, 500),
    new THREE.MeshStandardMaterial(),
  );
  const result = postValidate(THREE, mesh);
  assertRule('vertex limit exceeded (>250k)', result, 'VERTEX_LIMIT_EXCEEDED');
  assert('vertex limit — reports count', result.metrics == null || result.failures.some(f => f.detail.includes('>')));
}

{
  const lines = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(0.5, 0.5, 0.5)),
    new THREE.LineBasicMaterial(),
  );
  const result = postValidate(THREE, lines);
  assert('auto-wrap LineSegments passes', result.passed);
  assert('auto-wrap LineSegments — drawCalls == 1', result.metrics.drawCalls === 1);
}

{
  const line = new THREE.Line(
    new THREE.BufferGeometry().setAttribute(
      'position', new THREE.BufferAttribute(new Float32Array([0, 0, 0, 1, 0, 0]), 3),
    ),
    new THREE.LineBasicMaterial(),
  );
  const result = postValidate(THREE, line);
  assertRule('plain Line rejected as root', result, 'INVALID_RETURN_TYPE');
}

{
  const pts = new THREE.Points(
    new THREE.BufferGeometry().setAttribute(
      'position', new THREE.BufferAttribute(new Float32Array([0, 0, 0]), 3),
    ),
    new THREE.PointsMaterial({ size: 0.05 }),
  );
  const result = postValidate(THREE, pts);
  assert('auto-wrap Points passes', result.passed);
  assert('auto-wrap Points — drawCalls == 1', result.metrics.drawCalls === 1);
}

console.log(`\n=== Validation pool tests ===\n`);

await validationPool.init();

{
  const source = `export default function(THREE) {
    return new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.5, 0.5),
      new THREE.MeshStandardMaterial({ color: 0x2194ce })
    );
  }`;
  const result = await poolValidate(source);
  assert('pool: valid mesh passes', result.passed);
  assert('pool: valid mesh has metrics', result.metrics != null && result.metrics.vertices > 0);
}

{
  const source = `// This comment mentions export default function
export default function(THREE) {
    return new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.5, 0.5),
      new THREE.MeshStandardMaterial()
    );
  }`;
  const result = await poolValidate(source);
  assert('pool: export default in comment does not break transform', result.passed);
}

{
  const source = `export default function(THREE) {
    const label = "my export default thing";
    return new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.5, 0.5),
      new THREE.MeshStandardMaterial()
    );
  }`;
  const result = await poolValidate(source);
  assert('pool: export default in string does not break transform', result.passed);
}

{
  const source = `export default function(THREE) {
    return new THREE.Mesh(
      new THREE.BoxGeometry(3, 3, 3),
      new THREE.MeshStandardMaterial()
    );
  }`;
  const result = await poolValidate(source);
  assertRule('pool: oversized bbox rejected', result, 'BOUNDING_BOX_OUT_OF_RANGE');
}

{
  const source = `export default function(THREE) {
    return new THREE.Group();
  }`;
  const result = await poolValidate(source);
  assertRule('pool: empty scene rejected', result, 'EMPTY_SCENE');
}

{
  const source = `export default function(THREE) {
    const g = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial();
    for (let i = 0; i < 210; i++) {
      g.add(new THREE.Mesh(new THREE.BoxGeometry(0.01, 0.01, 0.01), mat));
    }
    return g;
  }`;
  const result = await poolValidate(source);
  assertRule('pool: draw call limit rejected', result, 'DRAW_CALL_LIMIT_EXCEEDED');
}

{
  const source = `export default function(THREE) {
    throw new Error("intentional failure");
  }`;
  const result = await poolValidate(source);
  assertRule('pool: throw during generate rejected', result, 'EXECUTION_THREW');
}

{
  const source = `export default function(THREE) {
    return 42;
  }`;
  const result = await poolValidate(source);
  assertRule('pool: non-Object3D return rejected', result, 'INVALID_RETURN_TYPE');
}

{
  const source = `export default function(THREE) {
    let parent = new THREE.Group();
    const root = parent;
    for (let i = 0; i < 35; i++) {
      const child = new THREE.Group();
      parent.add(child);
      parent = child;
    }
    parent.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), new THREE.MeshStandardMaterial()));
    return root;
  }`;
  const result = await poolValidate(source);
  assertRule('pool: depth limit rejected', result, 'DEPTH_LIMIT_EXCEEDED');
}

{
  const source = `export default function(THREE) {
    const geo = new THREE.BoxGeometry(0.05, 0.05, 0.05);
    const mat = new THREE.MeshStandardMaterial();
    const im = new THREE.InstancedMesh(geo, mat, 60000);
    im.count = 60000;
    return im;
  }`;
  const result = await poolValidate(source);
  assertRule('pool: instance limit rejected', result, 'INSTANCE_LIMIT_EXCEEDED');
}

{
  const source = `export default function(THREE) {
    return new THREE.Mesh(
      new THREE.SphereGeometry(0.4, 500, 500),
      new THREE.MeshStandardMaterial()
    );
  }`;
  const result = await poolValidate(source);
  assertRule('pool: vertex limit rejected', result, 'VERTEX_LIMIT_EXCEEDED');
}

{
  const source = `export default function(THREE) {
    const data = new Uint8Array(1100000);
    for (let i = 0; i < data.length; i++) data[i] = (i * 7) & 255;
    const tex = new THREE.DataTexture(data, 524, 524, THREE.RGBAFormat);
    const mat = new THREE.MeshStandardMaterial({ map: tex });
    return new THREE.Mesh(new THREE.PlaneGeometry(0.5, 0.5), mat);
  }`;
  const result = await poolValidate(source);
  assertRule('pool: texture data exceeded rejected', result, 'TEXTURE_DATA_EXCEEDED');
}

{
  const source = `export default function(THREE) { while(true) {} }`;
  const t0 = Date.now();
  const result = await poolValidate(source);
  const elapsed = Date.now() - t0;
  assertRule('pool: infinite loop times out', result, 'TIMEOUT_EXCEEDED');
  assert('pool: timeout within 10s', elapsed < 10000, `took ${elapsed}ms`);
}

{
  const source = `export default function(THREE) {
    return null;
  }`;
  const result = await poolValidate(source);
  assertRule('pool: null return rejected', result, 'INVALID_RETURN_TYPE');
}

console.log(`\n=== RUNTIME_VIOLATION tests ===\n`);

{
  const source = `export default function(THREE) {
    setTimeout(() => {}, 0);
    return new THREE.Group();
  }`;
  const result = await poolValidate(source);
  assertRule('pool: setTimeout triggers RUNTIME_VIOLATION', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
    fetch("http://evil.com");
    return new THREE.Group();
  }`;
  const result = await poolValidate(source);
  assertRule('pool: fetch triggers RUNTIME_VIOLATION', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
    setInterval(() => {}, 1000);
    return new THREE.Group();
  }`;
  const result = await poolValidate(source);
  assertRule('pool: setInterval triggers RUNTIME_VIOLATION', result, 'RUNTIME_VIOLATION');
}

console.log(`\n=== Codegen bypass tests ===\n`);

{
  const source = `export default function(THREE) {
  const F = (()=>{}).constructor;
  F('return 1')();
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: arrow.constructor blocked', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
  const F = "".constructor.constructor;
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: string.constructor.constructor blocked', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
  const GF = (function*(){}).constructor;
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: GeneratorFunction.constructor blocked', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
  const AF = (async()=>{}).constructor;
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: AsyncFunction.constructor blocked', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
  const F = (()=>{}).constructor;
  const ts = F('return Date.now()')();
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: constructor chain to Date.now() blocked', result, 'RUNTIME_VIOLATION');
}

console.log(`\n=== Expanded global trap tests ===\n`);

{
  const source = `export default function(THREE) {
  const d = Date.now();
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: Date trapped', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
  const t = performance.now();
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: performance trapped', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
  const v = process.version;
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: process trapped', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
  eval('1+1');
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: eval trapped (Phase 2)', result, 'RUNTIME_VIOLATION');
}

{
  const source = `export default function(THREE) {
  Function('return 1')();
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: Function trapped (Phase 2)', result, 'RUNTIME_VIOLATION');
}

console.log(`\n=== Top-level escape tests ===\n`);

{
  const source = `const x = setTimeout;
export default function(THREE) {
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: top-level setTimeout access caught by runtime trap', result, 'RUNTIME_VIOLATION');
}

{
  const source = `const f = fetch;
export default function(THREE) {
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: top-level fetch access caught by runtime trap', result, 'RUNTIME_VIOLATION');
}

{
  const source = `const F = (()=>{}).constructor;
export default function(THREE) {
  return new THREE.Mesh(new THREE.BoxGeometry(0.5,0.5,0.5), new THREE.MeshStandardMaterial());
}`;
  const result = await poolValidate(source);
  assertRule('pool: top-level constructor capture blocked', result, 'RUNTIME_VIOLATION');
}

console.log(`\n=== HEAP_EXCEEDED tests ===\n`);

{
  const source = `export default function(THREE) {
  const arr = [];
  while (true) arr.push(new Array(1000000).fill(0));
}`;
  const t0 = Date.now();
  const result = await poolValidate(source);
  const elapsed = Date.now() - t0;
  assertRule('pool: heap allocation triggers HEAP_EXCEEDED', result, 'HEAP_EXCEEDED');
  assert('pool: heap exceeded resolves within 30s', elapsed < 30000, `took ${elapsed}ms`);
}

console.log(`\n=== Pool lifecycle tests ===\n`);

{
  const source1 = `export default function(THREE) { while(true) {} }`;
  const result1 = await poolValidate(source1);
  assertRule('pool lifecycle: timeout kills worker', result1, 'TIMEOUT_EXCEEDED');

  await new Promise(r => setTimeout(r, 1000));

  const source2 = `export default function(THREE) {
    return new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.5, 0.5),
      new THREE.MeshStandardMaterial({ color: 0x2194ce })
    );
  }`;
  const result2 = await poolValidate(source2);
  assert('pool lifecycle: pool works after timeout kill', result2.passed);
}

{
  const source1 = `export default function(THREE) {
  const arr = [];
  while (true) arr.push(new Array(1000000).fill(0));
}`;
  const result1 = await poolValidate(source1);
  assertRule('pool lifecycle: heap kill detected', result1, 'HEAP_EXCEEDED');

  await new Promise(r => setTimeout(r, 1000));

  const source2 = `export default function(THREE) {
    return new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.5, 0.5),
      new THREE.MeshStandardMaterial({ color: 0x2194ce })
    );
  }`;
  const result2 = await poolValidate(source2);
  assert('pool lifecycle: pool works after heap kill', result2.passed);
}

{
  const promises = [];
  for (let i = 0; i < 4; i++) {
    promises.push(poolValidate(`export default function(THREE) {
      return new THREE.Mesh(
        new THREE.BoxGeometry(0.${i + 1}, 0.${i + 1}, 0.${i + 1}),
        new THREE.MeshStandardMaterial()
      );
    }`));
  }
  const results = await Promise.all(promises);
  const allPassed = results.every(r => r.passed);
  assert('pool lifecycle: concurrent requests all pass', allPassed,
    `${results.filter(r => !r.passed).length} of ${results.length} failed`);
}

const shutdownTimer = setTimeout(() => {
  console.error('FAIL shutdown — hung after 5s');
  process.exit(2);
}, 5000);

await validationPool.shutdown();
clearTimeout(shutdownTimer);
passed++;
console.log('\nPASS shutdown completes without hang');

console.log('');
console.log(`passed: ${passed}`);
console.log(`failed: ${failed}`);
console.log(`total:  ${passed + failed}`);
process.exitCode = failed > 0 ? 1 : 0;
