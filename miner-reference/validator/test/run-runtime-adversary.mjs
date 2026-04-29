/**
 * Runtime adversarial probe suite — complements test/adversary.test.mjs.
 *
 * Where adversary.test.mjs asserts the STATIC analyzer rejects each attack
 * pattern, this suite asserts that even if static analysis were entirely
 * bypassed, the runtime safeTHREE Proxy (plus the supporting language
 * semantics it relies on) catches every path that reaches a forbidden API.
 *
 * Discipline:
 *
 *   Each rejection case names the EXACT mechanism expected to trip — not
 *   just "it throws". Accepting any exception would let a case go green
 *   because it crashed for an unrelated reason before the Proxy was ever
 *   exercised, which would silently weaken the "un-bypassable" claim.
 *
 *   The three mechanisms, and the V8-stable messages they produce:
 *
 *     - 'proxy-get'
 *         Our Proxy's `get` trap throws one of the messages we construct:
 *         `THREE.<X> is forbidden`, `THREE.<X> is not a recognized Three.js
 *         API`, or the nested `THREE.<parent>.<X> is forbidden`. This is
 *         the core enforcement layer: any path that routes an allowed
 *         reference through our Proxy and then asks for a forbidden name
 *         must land here.
 *
 *     - 'proxy-mutation'
 *         Strict-mode mutation (`set` / `delete` / `defineProperty`) on a
 *         Proxy whose corresponding trap returns `false` produces a V8
 *         TypeError of the form `'<trap>' on proxy: trap returned falsish
 *         for property '<k>'`. This proves the Proxy is also locked down
 *         against mutation, not only read.
 *
 *     - 'undefined-constructor'
 *         Some attacks cannot reach our get trap at all because the Proxy
 *         hides the forbidden name from enumeration (`ownKeys`). Instead
 *         the attacker ends up with `undefined` and `new undefined(...)`
 *         raises the V8 TypeError `undefined is not a constructor`. This
 *         is still a blocked path; it just blocks via concealment rather
 *         than an explicit throw.
 *
 *   Cases marked `expect: 'ok'` MUST NOT throw — they exercise legitimate
 *   surface (allowed classes, MathUtils.clamp, Vector/Matrix math, etc.).
 *
 *   A throwing case whose actual error does NOT match its declared class
 *   fails the suite loudly, with a diagnostic printing the declared class
 *   and the actual message. This way, a regression that moves an attack
 *   from (say) proxy-get to undefined-constructor — which could indicate
 *   a subtly broken trap returning a stale value — shows up immediately
 *   rather than hiding under a permissive "some Error was raised" check.
 *
 * Run:  node test/runtime-adversary.test.mjs
 */

// NOTE: mirrored from render-service-js/test/runtime-adversary.test.mjs —
// keep in sync. The two runtime proxies are structurally identical, and
// this file protects the miner-side copy from drifting.

import * as THREE from 'three';
import {
  createSafeThree,
  SAFE_THREE_BROWSER_BUNDLE,
} from '../src/safeThree.js';
import {
  THREE_ALLOWED,
  THREE_BLOCKED_SUBMEMBERS,
  THREE_DISALLOWED_KNOWN,
} from '../src/threeAllowlist.js';

// Two entry points, one implementation. Miner-reference currently has no
// browser path, but we still reconstruct a proxy from the exported bundle
// string (the same one render-service-js ships into Puppeteer) and run
// every probe through BOTH factories. Any divergence between the direct
// and bundle-built proxies fails the suite loudly — this is the fence
// that keeps the two safeThree.js files (this one and
// render-service-js/src/safeThree.js) in lock step.
function buildFromBundle() {
  const cfg = {
    allowed: [...THREE_ALLOWED],
    disallowedKnown: [...THREE_DISALLOWED_KNOWN],
    blockedSubmembers: Object.fromEntries(
      Object.entries(THREE_BLOCKED_SUBMEMBERS).map(([k, v]) => [k, [...v]]),
    ),
  };
  const build = new Function(
    'THREE', 'cfg',
    `${SAFE_THREE_BROWSER_BUNDLE}\nreturn buildSafeThreeWithConfig(THREE, cfg);`,
  );
  return build(THREE, cfg);
}

const PROXIES = [
  { label: 'node',   proxy: createSafeThree(THREE) },
  { label: 'bundle', proxy: buildFromBundle() },
];

// ── expected-rejection classifiers ──────────────────────────────────────────
//
// `message` must be a perfectly reliable anchor — V8 owns the exact wording
// of the last two, so these patterns are chosen to match current output
// (checked against Node.js 20+ / V8 ≥ 12) while being loose enough to
// survive minor rephrasings. Update deliberately if they drift.

const REJECTIONS = {
  'proxy-get': {
    // Our own throws in src/safeThree.js — pinned by this test on purpose.
    // If these strings change, update this regex and update consuming
    // documentation alongside.
    message: /\bis forbidden\b|\bis not a recognized Three\.js API\b|\bforbidden names leaked\b/,
    type: TypeError,  // Error instances are fine; TypeError is a superset-friendly parent.
    acceptBaseError: true,
  },
  'proxy-mutation': {
    // V8-stable TypeError for a proxy trap returning falsish in strict mode.
    message: /'(set|deleteProperty|defineProperty)' on proxy: trap returned falsish/,
    type: TypeError,
  },
  'undefined-constructor': {
    // V8-stable TypeError when `new X(...)` is called on a non-constructor
    // value (commonly `undefined` in our suite because the Proxy hid the
    // forbidden name from enumeration).
    message: /\bis not a constructor\b/,
    type: TypeError,
  },
};

// Attack cases. `expect` is 'throw' (with `reject` naming the mechanism) or
// 'ok'. Keep the mechanism field lining up with the comments above — new
// cases should explicitly classify the path they exercise rather than
// leaving it to whatever crash happens to occur.
const cases = [
  // ── Launder THREE through every syntactic corner, then try ShaderMaterial ─

  { name: 'direct-access',                     expect: 'throw', reject: 'proxy-get', src: `
    return new THREE.ShaderMaterial({});
  ` },

  { name: 'alias-via-const',                   expect: 'throw', reject: 'proxy-get', src: `
    const X = THREE;
    return new X.ShaderMaterial({});
  ` },

  { name: 'object-destructure-shadermaterial', expect: 'throw', reject: 'proxy-get', src: `
    const { ShaderMaterial } = THREE;
    return new ShaderMaterial({});
  ` },

  { name: 'computed-property',                 expect: 'throw', reject: 'proxy-get', src: `
    const k = 'ShaderMaterial';
    return new THREE[k]({});
  ` },

  { name: 'logical-or-launder',                expect: 'throw', reject: 'proxy-get', src: `
    const X = THREE || {};
    return new X.ShaderMaterial({});
  ` },

  { name: 'ternary-launder',                   expect: 'throw', reject: 'proxy-get', src: `
    const X = true ? THREE : null;
    return new X.ShaderMaterial({});
  ` },

  { name: 'sequence-expression-launder',       expect: 'throw', reject: 'proxy-get', src: `
    const X = (0, THREE);
    return new X.ShaderMaterial({});
  ` },

  { name: 'for-of-over-array',                 expect: 'throw', reject: 'proxy-get', src: `
    for (const X of [THREE]) {
      return new X.ShaderMaterial({});
    }
  ` },

  { name: 'throw-and-catch-launder',           expect: 'throw', reject: 'proxy-get', src: `
    try { throw THREE; } catch (X) { return new X.ShaderMaterial({}); }
  ` },

  { name: 'tagged-template-launder',           expect: 'throw', reject: 'proxy-get', src: `
    const tag = (strings, ...values) => values[0];
    const X = tag\`$\${THREE}\`;
    return new X.ShaderMaterial({});
  ` },

  { name: 'generator-yield-launder',           expect: 'throw', reject: 'proxy-get', src: `
    function* gen() { yield THREE; }
    const X = gen().next().value;
    return new X.ShaderMaterial({});
  ` },

  // ── Assignment-target laundering (LHS-shape family) ──────────────────────

  { name: 'stash-on-this',                     expect: 'throw', reject: 'proxy-get', src: `
    class B {
      constructor(T) { this.t = T; }
      build() { return new this.t.ShaderMaterial({}); }
    }
    return new B(THREE).build();
  ` },

  { name: 'stash-on-plain-object',             expect: 'throw', reject: 'proxy-get', src: `
    const obj = { t: null };
    obj.t = THREE;
    return new obj.t.ShaderMaterial({});
  ` },

  { name: 'destructure-into-member-target',    expect: 'throw', reject: 'proxy-get', src: `
    const obj = {};
    ({ ShaderMaterial: obj.sm } = THREE);
    return new obj.sm({});
  ` },

  { name: 'array-destructure-with-default',    expect: 'throw', reject: 'proxy-get', src: `
    const obj = {};
    [obj.sm = THREE.ShaderMaterial] = [];
    return new obj.sm({});
  ` },

  { name: 'class-field-initializer',           expect: 'throw', reject: 'proxy-get', src: `
    class H {
      constructor(T) { this.three = T; }
      build() { return new this.three.ShaderMaterial({}); }
    }
    return new H(THREE).build();
  ` },

  // ── Call-forwarding (helper / IIFE / default-param) ───────────────────────

  { name: 'helper-param-destructured',         expect: 'throw', reject: 'proxy-get', src: `
    const use = ({ ShaderMaterial }) => new ShaderMaterial({});
    return use(THREE);
  ` },

  { name: 'helper-param-renamed',              expect: 'throw', reject: 'proxy-get', src: `
    function make(t) { return new t.ShaderMaterial({}); }
    return make(THREE);
  ` },

  { name: 'helper-rest-param-capture',         expect: 'throw', reject: 'proxy-get', src: `
    function h(...args) { const t = args[0]; return new t.ShaderMaterial({}); }
    return h(THREE);
  ` },

  { name: 'default-param-is-three',            expect: 'throw', reject: 'proxy-get', src: `
    const h = (t = THREE) => new t.ShaderMaterial({});
    return h();
  ` },

  { name: 'iife-destructured-param',           expect: 'throw', reject: 'proxy-get', src: `
    return (({ ShaderMaterial }) => new ShaderMaterial({}))(THREE);
  ` },

  { name: 'class-ctor-renamed-param',          expect: 'throw', reject: 'proxy-get', src: `
    class B {
      constructor(T) { this.T = T; }
      build() { return new this.T.ShaderMaterial({}); }
    }
    return new B(THREE).build();
  ` },

  { name: 'method-call-with-three-arg',        expect: 'throw', reject: 'proxy-get', src: `
    function use(t) { return new t.ShaderMaterial({}); }
    return use.call(null, THREE);
  ` },

  // ── Reflective enumeration: Proxy must hide forbidden names ──────────────

  { name: 'enumeration-hides-forbidden',       expect: 'throw', reject: 'proxy-get', src: `
    const names = Object.getOwnPropertyNames(THREE);
    const leaked = ['ShaderMaterial','WebGLRenderer','TextureLoader']
      .filter(n => names.includes(n));
    if (leaked.length) {
      // TypeError keeps the error class consistent with the Proxy's
      // own throws, so the matcher below applies uniformly.
      throw new TypeError('forbidden names leaked via enumeration: ' + leaked.join(','));
    }
    return new THREE.ShaderMaterial({});
  ` },

  // 'Object.assign' copies only what ownKeys exposes, so the target lacks
  // the forbidden key entirely — the attack dies at \`new undefined(...)\`,
  // not at our get trap. That's the mechanism we pin here.
  { name: 'object-assign-laundering',          expect: 'throw', reject: 'undefined-constructor', src: `
    const target = Object.assign({}, THREE);
    return new target.ShaderMaterial({});
  ` },

  { name: 'in-operator-check-then-access',     expect: 'throw', reject: 'proxy-get', src: `
    if ('ShaderMaterial' in THREE) { /* unreachable */ }
    return new THREE.ShaderMaterial({});
  ` },

  // ── MathUtils sub-proxy: seededRandom / generateUUID are blocked ──────────

  { name: 'mathutils-seededRandom-direct',     expect: 'throw', reject: 'proxy-get', src: `
    return THREE.MathUtils.seededRandom(1);
  ` },

  { name: 'mathutils-generateUUID-direct',     expect: 'throw', reject: 'proxy-get', src: `
    return THREE.MathUtils.generateUUID();
  ` },

  { name: 'mathutils-destructure-blocked',     expect: 'throw', reject: 'proxy-get', src: `
    const { MathUtils } = THREE;
    return MathUtils.seededRandom(42);
  ` },

  { name: 'mathutils-nested-destructure-blocked', expect: 'throw', reject: 'proxy-get', src: `
    const { MathUtils: { seededRandom } } = THREE;
    return seededRandom(42);
  ` },

  // ── Mutation traps: every attempt to mutate the namespace is rejected ────

  { name: 'assign-through-proxy-rejected',     expect: 'throw', reject: 'proxy-mutation', src: `
    THREE.ShaderMaterial = function FakeSM() {};
    return new THREE.ShaderMaterial({});
  ` },

  { name: 'delete-on-proxy-rejected',          expect: 'throw', reject: 'proxy-mutation', src: `
    delete THREE.Mesh;
    return new THREE.ShaderMaterial({});
  ` },

  { name: 'defineproperty-on-proxy-rejected',  expect: 'throw', reject: 'proxy-mutation', src: `
    Object.defineProperty(THREE, 'ShaderMaterial', { value: function FakeSM(){}, configurable: true });
    return new THREE.ShaderMaterial({});
  ` },

  // ── Legitimate usage — must keep working through the Proxy ───────────────

  { name: 'legit-minimal-mesh',                expect: 'ok', src: `
    return new THREE.Mesh(
      new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshStandardMaterial(),
    );
  ` },

  { name: 'legit-named-destructure',           expect: 'ok', src: `
    const { Mesh, BoxGeometry, MeshStandardMaterial, Group } = THREE;
    const g = new Group();
    g.add(new Mesh(new BoxGeometry(1, 1, 1), new MeshStandardMaterial()));
    return g;
  ` },

  { name: 'legit-mathutils-clamp',             expect: 'ok', src: `
    const v = THREE.MathUtils.clamp(1.5, 0, 1);
    return new THREE.Mesh(
      new THREE.BoxGeometry(v, v, v),
      new THREE.MeshStandardMaterial(),
    );
  ` },

  { name: 'legit-mathutils-nested-destructure', expect: 'ok', src: `
    const { MathUtils: { clamp, lerp } } = THREE;
    const v = clamp(lerp(0, 2, 0.5), 0, 1);
    return new THREE.Mesh(
      new THREE.BoxGeometry(v, v, v),
      new THREE.MeshStandardMaterial(),
    );
  ` },

  { name: 'legit-helper-named-THREE',          expect: 'ok', src: `
    function size(THREE, root) {
      return new THREE.Box3().setFromObject(root).getSize(new THREE.Vector3());
    }
    const g = new THREE.Group();
    g.add(new THREE.Mesh(new THREE.BoxGeometry(1,1,1), new THREE.MeshStandardMaterial()));
    size(THREE, g);
    return g;
  ` },

  { name: 'legit-vector-math',                 expect: 'ok', src: `
    const v = new THREE.Vector3(1, 2, 3);
    v.normalize();
    const q = new THREE.Quaternion().setFromAxisAngle(v, Math.PI / 4);
    const m = new THREE.Matrix4().makeRotationFromQuaternion(q);
    const g = new THREE.Group();
    g.applyMatrix4(m);
    g.add(new THREE.Mesh(new THREE.SphereGeometry(0.5), new THREE.MeshBasicMaterial()));
    return g;
  ` },
];

// ── runner ──────────────────────────────────────────────────────────────────

function classify(err, expectedReject) {
  const spec = REJECTIONS[expectedReject];
  if (!spec) {
    return { matches: false, reason: `unknown reject classifier '${expectedReject}'` };
  }
  const msg = err && err.message ? String(err.message) : '';
  if (!spec.message.test(msg)) {
    return {
      matches: false,
      reason: `message did not match ${spec.message} — got: ${JSON.stringify(msg)}`,
    };
  }
  if (!(err instanceof spec.type)) {
    return {
      matches: false,
      reason: `expected ${spec.type.name}, got ${err && err.constructor ? err.constructor.name : typeof err}`,
    };
  }
  return { matches: true };
}

function runCase(tc, entry) {
  const { expect, src } = tc;
  const { proxy } = entry;

  // Module bodies are implicitly strict; we make that explicit so the
  // mutation-rejection cases see the TypeError they expect.
  const fn = new Function('THREE', `'use strict';\n${src}`);

  let threw = null;
  try {
    fn(proxy);
  } catch (err) {
    threw = err;
  }

  if (expect === 'ok') {
    if (!threw) return { pass: true };
    return {
      pass: false,
      reason: `expected: ok (legitimate pattern) — got ${threw.constructor?.name || typeof threw}: ${threw.message}`,
    };
  }

  if (!threw) {
    return { pass: false, reason: `expected reject class ${tc.reject} — got no throw (possible bypass)` };
  }

  const { matches, reason } = classify(threw, tc.reject);
  if (matches) return { pass: true };
  return {
    pass: false,
    reason: `expected reject class ${tc.reject} — ${reason}; actual ${threw.constructor?.name || typeof threw}: ${threw.message}`,
  };
}

let passed = 0;
let failed = 0;

for (const tc of cases) {
  // Run every case against every safeTHREE entry point (direct + bundle-
  // rebuilt). A case only passes when BOTH paths agree — divergence
  // between `createSafeThree` and `buildSafeThreeWithConfig` reconstructed
  // from the exported bundle indicates the two sources have drifted.
  const results = PROXIES.map((entry) => ({
    entry,
    result: runCase(tc, entry),
  }));

  const allPassed = results.every((r) => r.result.pass);
  if (allPassed) {
    passed++;
    continue;
  }

  failed++;
  console.error(`FAIL ${tc.name}`);
  for (const { entry, result } of results) {
    if (result.pass) {
      console.error(`  [${entry.label}]   ok`);
    } else {
      console.error(`  [${entry.label}]   ${result.reason}`);
    }
  }
}

console.log(`\nruntime adversary suite (${PROXIES.map((p) => p.label).join(' + ')})`);
console.log(`passed: ${passed}`);
console.log(`failed: ${failed}`);
console.log(`total:  ${cases.length}`);

if (failed > 0) process.exit(1);
