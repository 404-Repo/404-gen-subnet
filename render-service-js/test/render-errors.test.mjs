/** Local HTTP and worker checks; no Chromium or external service is started. */
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { server } from '../src/server.js';
import { validationPool, ValidationUnavailableError } from '../src/validation-pool.js';
import { staticValidate } from '../src/validator/index.js';
import { boundValidationResult, MAX_FAILURES, MAX_DETAIL_BYTES, MAX_REPORT_BYTES } from '../src/validation-report.js';

const VALID = `export default function generate(THREE) {
  return new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5), new THREE.MeshStandardMaterial());
}`;
const LOOP = 'export default function generate(THREE) { while (true) {} }';
const ROUTES = ['/render', '/render/grid'];

await validationPool.init();
const poolSize = validationPool.workers.length;
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
after(async () => {
  const closed = new Promise((resolve) => server.close(resolve));
  server.closeAllConnections();
  await closed;
  await validationPool.shutdown();
});

async function post(route, source) {
  const response = await fetch(base + route, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source }),
    signal: AbortSignal.timeout(15000),
  });
  assert.equal(response.headers.get('content-type'), 'application/json');
  const text = await response.text();
  assert.ok(Buffer.byteLength(text) <= MAX_REPORT_BYTES);
  return { status: response.status, body: JSON.parse(text) };
}

async function waitFor(predicate) {
  const deadline = Date.now() + 3000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, 'worker lifecycle operation did not finish');
    await delay(10);
  }
}

async function resetValidationPool() {
  await validationPool.shutdown();
  await waitFor(() => validationPool._replacing === 0);
  await validationPool.init();
}

test('render routes return the same validation rejections and metrics', async () => {
  const fixtures = [
    ['export default function(THREE) {\n  const x = ;\n}', 'PARSE_ERROR'],
    ['export default function(THREE) { fetch("asset"); Math.random(); return new THREE.PyramidGeometry(); }', 'FORBIDDEN_IDENTIFIER'],
    ['export default function(THREE) { throw new Error("boom"); }', 'EXECUTION_THREW'],
    ['export default function(THREE) { throw null; }', 'EXECUTION_THREW'],
    ['export default function(THREE) { throw { get message() { throw 42; } }; }', 'EXECUTION_THREW'],
    ['export default function(THREE) { return null; }', 'INVALID_RETURN_TYPE'],
    ['export default function(THREE) { return new THREE.Group(); }', 'EMPTY_SCENE'],
    [VALID.replace('0.5, 0.5, 0.5', '3, 3, 3'), 'BOUNDING_BOX_OUT_OF_RANGE'],
  ];
  for (const [source, rule] of fixtures) {
    const expected = await post('/render', source);
    assert.equal(expected.status, 422);
    assert.ok(expected.body.failures.some((failure) => failure.rule === rule));
    for (const route of ROUTES.slice(1)) assert.deepEqual(await post(route, source), expected);
  }
});

test('render diagnostic count is bounded', async () => {
  const source = `export default function(THREE) { ${'fetch;'.repeat(100)} return null; }`;
  for (const route of ROUTES) {
    const { status, body } = await post(route, source);
    assert.equal(status, 422);
    assert.equal(body.failures.length, MAX_FAILURES);
    assert.equal(body.truncated, true);
  }
});

test('workers bound exception details before returning them to the parent', async () => {
  const source = 'export default function(THREE) { throw new Error("🧱".repeat(10000)); }';
  const result = await validationPool.validate(staticValidate(source).transformed);
  assert.equal(result.passed, false);
  assert.equal(result.truncated, true);
  assert.ok(Buffer.byteLength(result.failures[0].detail) <= MAX_DETAIL_BYTES);
  assert.ok(!result.failures[0].detail.includes('\ufffd'));
  const response = await post('/render', source);
  assert.equal(response.status, 422);
  assert.deepEqual(response.body.failures, result.failures);
  assert.equal(response.body.truncated, true);
});

test('inherited error values cannot enlarge worker reports or discard failures', async (t) => {
  t.after(resetValidationPool);
  const mutation = 'try { Object.prototype.error = "x".repeat(100000); } catch {}';
  const source = `export default function(THREE) {
    ${mutation}
    throw new Error("miner exception");
  }`;
  const failures = [{ stage: 'execution', rule: 'EXECUTION_THREW', detail: 'miner exception' }];
  const report = await validationPool.validate(staticValidate(source).transformed);
  assert.ok(Buffer.byteLength(JSON.stringify(report)) <= MAX_REPORT_BYTES);
  assert.deepEqual(report.failures, failures);
  for (const route of ROUTES) {
    const { status, body } = await post(route, source);
    assert.equal(status, 422);
    assert.deepEqual(body.failures, failures);
  }
  const passingSource = VALID.replace('{', `{ ${mutation}`);
  const passingReport = await validationPool.validate(staticValidate(passingSource).transformed);
  assert.equal(passingReport.passed, true);
  assert.ok(Buffer.byteLength(JSON.stringify(passingReport)) <= MAX_REPORT_BYTES);
});

test('miner overrides cannot break diagnostic normalization', async (t) => {
  t.after(resetValidationPool);
  for (const mutation of [
    'String.prototype.slice = function() { throw new Error("slice hook"); };',
    'Math.min = function() { throw new Error("min hook"); };',
    'Boolean = function() { throw new Error("boolean hook"); };',
    'Object.defineProperty(Object.prototype, "truncated", { get() { throw new Error("truncated hook"); }, configurable: true });',
  ]) {
    const source = `export default function(THREE) {
      try { ${mutation} } catch {}
      throw new Error("miner exception");
    }`;
    for (const route of ROUTES) {
      const { status, body } = await post(route, source);
      assert.equal(status, 422);
      assert.equal(body.error, 'post_validation_failed');
      assert.deepEqual(body.failures, [{
        stage: 'execution', rule: 'EXECUTION_THREW', detail: 'miner exception',
      }]);
    }
  }
});

test('miner serialization hooks preserve the original validation rejection', async (t) => {
  t.after(resetValidationPool);
  for (const mutation of [
    'Object.prototype.toJSON = function() { throw new Error("serialization trap"); };',
    'Array.prototype.toJSON = function() { throw new Error("serialization trap"); };',
    'JSON.stringify = function() { throw new Error("serialization trap"); };',
  ]) {
    const source = `export default function(THREE) {
      try { ${mutation} } catch {}
      throw new Error("miner exception");
    }`;
    for (const route of ROUTES) {
      const { status, body } = await post(route, source);
      assert.equal(status, 422);
      assert.equal(body.error, 'post_validation_failed');
      assert.deepEqual(body.failures, [{
        stage: 'execution', rule: 'EXECUTION_THREW', detail: 'miner exception',
      }]);
    }
  }
});

test('uncaught shared-state writes are 422 rejections on both render routes', async (t) => {
  t.after(resetValidationPool);
  for (const mutation of [
    'THREE.Box3.prototype.setFromObject = function() { return this; };',
    'Array.prototype.push = function() { throw new Error("poisoned push"); };',
  ]) {
    const source = `export default function(THREE) {
      ${mutation}
      return null;
    }`;
    for (const route of ROUTES) {
      const { status, body } = await post(route, source);
      assert.equal(status, 422);
      assert.equal(body.error, 'post_validation_failed');
      assert.equal(body.failures[0].stage, 'execution');
    }
  }
  assert.equal((await validationPool.validate(staticValidate(VALID).transformed)).passed, true);
});

test('total JSON bound accounts for control-character escaping', () => {
  const report = boundValidationResult({
    passed: false, metrics: null,
    failures: Array.from({ length: MAX_FAILURES }, () => ({
      stage: 'execution', rule: 'EXECUTION_THREW', detail: '\u0000'.repeat(MAX_DETAIL_BYTES),
    })),
  });
  assert.ok(Buffer.byteLength(JSON.stringify(report)) <= MAX_REPORT_BYTES);
  assert.ok(report.failures.length > 0 && report.failures.length < MAX_FAILURES);
  assert.equal(report.truncated, true);
});

test('execution timeouts and confirmed heap limits remain miner rejections', async () => {
  const timeout = await post('/render', LOOP);
  assert.equal(timeout.status, 422);
  assert.equal(timeout.body.failures[0].rule, 'TIMEOUT_EXCEEDED');
  const heap = await post('/render/grid', `export default function(THREE) {
    const values = []; while (true) values.push(new Array(1000000).fill(0));
  }`);
  assert.equal(heap.status, 422);
  assert.equal(heap.body.failures[0].rule, 'HEAP_EXCEEDED');
  assert.equal((await validationPool.validate(staticValidate(VALID).transformed)).passed, true);
  await waitFor(() => validationPool.available.length === poolSize);
});

test('unexpected worker exits and errors are 503 on both render routes, then recover', async () => {
  for (const [index, route] of ROUTES.entries()) {
    const pending = post(route, LOOP);
    await waitFor(() => validationPool.workers.some((worker) => worker._pending.size > 0));
    const worker = validationPool.workers.find((candidate) => candidate._pending.size > 0);
    if (index === 0) worker.emit('error', new Error('private infrastructure fixture'));
    await worker.terminate();
    assert.deepEqual(await pending, { status: 503, body: { error: 'validation_unavailable' } });
    await waitFor(() => validationPool.available.length === poolSize);
    assert.equal((await validationPool.validate(staticValidate(VALID).transformed)).passed, true);
  }
});

test('replacement failure rejects queued work instead of leaving it pending', async () => {
  const original = validationPool._createWorker;
  const transformed = staticValidate(LOOP).transformed;
  const pending = Array.from({ length: poolSize + 1 }, () =>
    validationPool.validate(transformed).catch((error) => error));
  await waitFor(() => validationPool.waiting.length === 1);
  validationPool._createWorker = async () => { throw new Error('replacement failure fixture'); };
  try {
    await Promise.all([...validationPool.workers].map((worker) => worker.terminate()));
    for (const result of await Promise.all(pending)) assert.ok(result instanceof ValidationUnavailableError);
    assert.deepEqual(await post('/render', VALID), { status: 503, body: { error: 'validation_unavailable' } });
  } finally {
    validationPool._createWorker = original;
    await validationPool.shutdown();
    await validationPool.init();
  }
});
