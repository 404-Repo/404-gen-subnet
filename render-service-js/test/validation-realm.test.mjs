/** Shared-state mutation must not change this request's verdict or later requests. */
import assert from 'node:assert/strict';
import { beforeEach, afterEach, test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { staticValidate } from '../src/validator/index.js';

process.env.VALIDATION_POOL_SIZE = '1';
const { validationPool } = await import('../src/validation-pool.js');

const cube = (size) => `export default function(THREE) {
  return new THREE.Mesh(new THREE.BoxGeometry(${size}, ${size}, ${size}), new THREE.MeshStandardMaterial());
}`;
const spoof = `function() {
  this.min.set(-0.1, -0.1, -0.1);
  this.max.set(0.1, 0.1, 0.1);
  THREE.Box3.prototype.setFromObject = original;
  return this;
}`;

beforeEach(async () => { await validationPool.init(); });
afterEach(async () => {
  await validationPool.shutdown();
  const deadline = Date.now() + 3000;
  while (validationPool._replacing > 0) {
    assert.ok(Date.now() < deadline, 'worker replacement did not finish');
    await delay(10);
  }
});

async function validate(source) {
  const parsed = staticValidate(source);
  assert.equal(parsed.passed, true, JSON.stringify(parsed.failures));
  return validationPool.validate(parsed.transformed);
}

function assertOversized(result) {
  assert.equal(result.passed, false);
  assert.ok(result.failures.some((failure) => failure.rule === 'BOUNDING_BOX_OUT_OF_RANGE'));
  assert.equal(result.metrics.bbox.min.x, -1.5);
  assert.equal(result.metrics.bbox.max.x, 1.5);
}

test('same-request prototype spoofing cannot conceal an oversized scene', async () => {
  assertOversized(await validate(cube(3)));
  const result = await validate(`export default function(THREE) {
    const original = THREE.Box3.prototype.setFromObject;
    try { THREE.Box3.prototype.setFromObject = ${spoof}; } catch {}
    return new THREE.Mesh(new THREE.BoxGeometry(3, 3, 3), new THREE.MeshStandardMaterial());
  }`);
  assertOversized(result);
});

test('mutation during post-validation cannot spoof bounds and restore itself', async () => {
  const result = await validate(`export default function(THREE) {
    const root = new THREE.Mesh(new THREE.BoxGeometry(3, 3, 3), new THREE.MeshStandardMaterial());
    const original = THREE.Box3.prototype.setFromObject;
    Object.defineProperty(root, 'isGroup', { get() {
      try { THREE.Box3.prototype.setFromObject = ${spoof}; } catch {}
      return false;
    } });
    return root;
  }`);
  assertOversized(result);
});

test('cross-request Three.js poisoning cannot change later scene verdicts', async () => {
  const attack = await validate(`export default function(THREE) {
    THREE.Box3.prototype.setFromObject = function() {
      this.min.set(-0.1, -0.1, -0.1);
      this.max.set(0.1, 0.1, 0.1);
      return this;
    };
    return new THREE.Mesh(new THREE.BoxGeometry(3, 3, 3), new THREE.MeshStandardMaterial());
  }`);
  const valid = await validate(cube(0.5));
  const invalid = await validate(cube(3));
  assert.equal(attack.passed, false);
  assert.equal(valid.passed, true);
  assertOversized(invalid);
});

test('cross-request intrinsic poisoning cannot reject a later valid scene', async () => {
  for (const mutation of [
    'Array.prototype.push = function() { throw new Error("poisoned push"); };',
    'Math.min = function() { throw new Error("poisoned min"); };',
    'Set.prototype.has = function() { return true; };',
    'Math = {};',
    'Object = {};',
    'Object.getPrototypeOf([1].values()).next = function() { throw new Error("poisoned iterator"); };',
  ]) {
    const attack = await validate(`export default function(THREE) {
      ${mutation}
      throw new Error("miner exception");
    }`);
    assert.equal(attack.passed, false);
    assert.equal((await validate(cube(0.5))).passed, true);
    assertOversized(await validate(cube(3)));
  }
});

test('protected math prototypes still support ordinary construction and cloning', async () => {
  const source = `export default function(THREE) {
    new THREE.Vector2(1, 2).clone();
    new THREE.Vector3(1, 2, 3).clone();
    new THREE.Vector4(1, 2, 3, 4).clone();
    new THREE.Matrix3().identity().clone();
    new THREE.Matrix4().identity().clone();
    return new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5), new THREE.MeshStandardMaterial());
  }`;
  const first = await validate(source);
  const second = await validate(source);
  assert.equal(first.passed, true);
  assert.equal(second.passed, true);
  assert.deepEqual(second.metrics, first.metrics);
});
