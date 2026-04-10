/**
 * Regression tests for the static analysis validator.
 *
 * Run:  node test/validator.test.mjs
 *       npm test
 *
 * Each test case is { name, source, expect } where expect is either:
 *   "pass"                     — validator should accept the source
 *   { rule: "RULE_CODE" }     — validator should reject with this rule
 *   { rule: "RULE_CODE", detail: /regex/ } — rule + detail pattern
 */

import { staticValidate } from '../src/validator/index.js';

let passed = 0;
let failed = 0;

function test(name, source, expect) {
  const result = staticValidate(source);

  if (expect === 'pass') {
    if (result.passed) {
      passed++;
    } else {
      failed++;
      console.error(`FAIL ${name}`);
      console.error(`  expected: pass`);
      console.error(`  got: ${result.failures[0]?.rule} — ${result.failures[0]?.detail}`);
    }
    return;
  }

  if (!result.passed) {
    const f = result.failures[0];
    const ruleOk = f.rule === expect.rule;
    const detailOk = !expect.detail || expect.detail.test(f.detail);
    if (ruleOk && detailOk) {
      passed++;
    } else {
      failed++;
      console.error(`FAIL ${name}`);
      console.error(`  expected rule: ${expect.rule}${expect.detail ? `, detail: ${expect.detail}` : ''}`);
      console.error(`  got: ${f.rule} — ${f.detail}`);
    }
  } else {
    failed++;
    console.error(`FAIL ${name}`);
    console.error(`  expected: reject with ${expect.rule}`);
    console.error(`  got: pass`);
  }
}

// ─── Valid sources (should pass) ─────────────────────────────────────────────

test('minimal mesh',
  `export default function(THREE) {
    return new THREE.Mesh(
      new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshStandardMaterial({ color: 0x2194ce })
    );
  }`,
  'pass');

test('group with children',
  `export default function(THREE) {
    const g = new THREE.Group();
    g.add(new THREE.Mesh(new THREE.SphereGeometry(0.3), new THREE.MeshStandardMaterial()));
    g.add(new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.5, 0.5), new THREE.MeshBasicMaterial()));
    return g;
  }`,
  'pass');

test('helper function receiving THREE',
  `function makeBox(THREE) {
    return new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial());
  }
  export default function(THREE) { return makeBox(THREE); }`,
  'pass');

test('class syntax',
  `class Builder {
    constructor(THREE) { this.scene = new THREE.Group(); }
    build() { return this.scene; }
  }
  export default function(THREE) { return new Builder(THREE).build(); }`,
  'pass');

// Helper-param hardening — THREE may only be forwarded to a callee whose
// matched parameter is exactly named THREE. Anything else (renamed
// identifier, destructured pattern, rest parameter, unresolvable callee)
// is rejected, because the analyzer cannot see through the rebind and the
// allowlist would be bypassed inside the helper.
test('helper with renamed THREE param (bypass closed)',
  `function makeBox(T) {
    return new T.Mesh(new T.BoxGeometry(1, 1, 1), new T.MeshStandardMaterial());
  }
  export default function(THREE) { return makeBox(THREE); }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('helper with destructured param (bypass closed)',
  `export default function(THREE) {
    const use = ({ Mesh, BoxGeometry, ShaderMaterial }) =>
      new Mesh(new BoxGeometry(0.1, 0.1, 0.1), new ShaderMaterial({ vertexShader: '', fragmentShader: '' }));
    return use(THREE);
  }`,
  { rule: 'FORBIDDEN_THREE_API' });

test('IIFE destructured param (bypass closed)',
  `export default function(THREE) {
    return (({ Mesh, BoxGeometry, ShaderMaterial }) =>
      new Mesh(new BoxGeometry(0.1, 0.1, 0.1), new ShaderMaterial({ vertexShader: '', fragmentShader: '' })))(THREE);
  }`,
  { rule: 'FORBIDDEN_THREE_API' });

test('class with renamed ctor param (bypass closed)',
  `class Builder {
    constructor(T) { this.T = T; }
    build() { return new this.T.Group(); }
  }
  export default function(THREE) { return new Builder(THREE).build(); }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('helper with rest param capturing THREE',
  `function helper(...args) {
    const t = args[0];
    return new t.Mesh(new t.BoxGeometry(0.1, 0.1, 0.1), new t.MeshStandardMaterial());
  }
  export default function(THREE) { return helper(THREE); }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('THREE as default parameter value',
  `export default function(THREE) {
    const helper = (t = THREE) =>
      new t.Mesh(new t.BoxGeometry(0.1, 0.1, 0.1), new t.MeshStandardMaterial());
    return helper();
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('return THREE from helper',
  `export default function(THREE) {
    const leak = () => THREE;
    const X = leak();
    return new X.Mesh(new X.BoxGeometry(0.1, 0.1, 0.1), new X.MeshStandardMaterial());
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('stash THREE in container',
  `export default function(THREE) {
    const holder = { t: THREE };
    return new holder.t.Mesh(new holder.t.BoxGeometry(0.1, 0.1, 0.1), new holder.t.MeshStandardMaterial());
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('THREE in array literal',
  `export default function(THREE) {
    const arr = [THREE];
    return new arr[0].Mesh(new arr[0].BoxGeometry(0.1, 0.1, 0.1), new arr[0].MeshStandardMaterial());
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('THREE via method call (.call)',
  `export default function(THREE) {
    function use(t) { return new t.Mesh(new t.BoxGeometry(0.1, 0.1, 0.1), new t.MeshStandardMaterial()); }
    return use.call(null, THREE);
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('stash THREE on this.x (bypass closed)',
  `class Builder {
    constructor(THREE) { this.t = THREE; }
    build() { return new this.t.ShaderMaterial({}); }
  }
  export default function(THREE) { return new Builder(THREE).build(); }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('stash THREE on obj.x (bypass closed)',
  `export default function(THREE) {
    const obj = { t: null };
    obj.t = THREE;
    return new obj.t.ShaderMaterial({});
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN' });

test('destructure with member-expression target (forbidden key)',
  `export default function(THREE) {
    const obj = {};
    ({ ShaderMaterial: obj.sm } = THREE);
    return new obj.sm({});
  }`,
  { rule: 'FORBIDDEN_THREE_API' });

test('generator function (sync)',
  `function* range(n) { for (let i = 0; i < n; i++) yield i; }
  export default function(THREE) {
    const g = new THREE.Group();
    for (const i of range(3)) {
      const m = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.2, 0.2), new THREE.MeshStandardMaterial());
      m.position.set(i * 0.3, 0, 0);
      g.add(m);
    }
    return g;
  }`,
  'pass');

test('typed arrays and math',
  `export default function(THREE) {
    const positions = new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0]);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    return new THREE.Mesh(geo, new THREE.MeshStandardMaterial());
  }`,
  'pass');

test('all allowed primitives',
  `export default function(THREE) {
    const g = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial();
    g.add(new THREE.Mesh(new THREE.BoxGeometry(), mat));
    g.add(new THREE.Mesh(new THREE.SphereGeometry(), mat));
    g.add(new THREE.Mesh(new THREE.CylinderGeometry(), mat));
    g.add(new THREE.Mesh(new THREE.ConeGeometry(), mat));
    g.add(new THREE.Mesh(new THREE.TorusGeometry(), mat));
    g.add(new THREE.Mesh(new THREE.PlaneGeometry(), mat));
    return g;
  }`,
  'pass');

test('instanced mesh',
  `export default function(THREE) {
    const geo = new THREE.BoxGeometry(0.1, 0.1, 0.1);
    const mat = new THREE.MeshStandardMaterial({ color: 0xff0000 });
    const im = new THREE.InstancedMesh(geo, mat, 100);
    const dummy = new THREE.Object3D();
    for (let i = 0; i < 100; i++) {
      dummy.position.set(Math.sin(i), Math.cos(i), 0);
      dummy.updateMatrix();
      im.setMatrixAt(i, dummy.matrix);
    }
    return im;
  }`,
  'pass');

test('data texture',
  `export default function(THREE) {
    const data = new Uint8Array(4 * 4 * 4);
    for (let i = 0; i < data.length; i++) data[i] = 128;
    const tex = new THREE.DataTexture(data, 4, 4, THREE.RGBAFormat);
    const mat = new THREE.MeshStandardMaterial({ map: tex });
    return new THREE.Mesh(new THREE.PlaneGeometry(1, 1), mat);
  }`,
  'pass');

test('destructuring in parameters',
  `export default function(THREE) {
    const build = ({ width, height }) => {
      return new THREE.Mesh(
        new THREE.BoxGeometry(width, height, 0.1),
        new THREE.MeshStandardMaterial()
      );
    };
    return build({ width: 0.5, height: 0.3 });
  }`,
  'pass');

// ─── Structural checks ──────────────────────────────────────────────────────

test('no export default',
  `function generate(THREE) { return new THREE.Group(); }`,
  { rule: 'MISSING_DEFAULT_EXPORT' });

test('export default is not a function',
  `export default 42;`,
  { rule: 'MISSING_DEFAULT_EXPORT' });

test('multiple top-level exports',
  `export const foo = 1;
  export default function(THREE) { return new THREE.Group(); }`,
  { rule: 'MULTIPLE_TOP_LEVEL_EXPORTS' });

test('async default export',
  `export default async function(THREE) { return new THREE.Group(); }`,
  { rule: 'ASYNC_NOT_ALLOWED' });

test('nested async function',
  `export default function(THREE) {
    async function helper() { return 1; }
    return new THREE.Group();
  }`,
  { rule: 'ASYNC_NOT_ALLOWED' });

test('for await loop',
  `export default function(THREE) {
    async function run() { for await (const x of []) {} }
    return new THREE.Group();
  }`,
  { rule: 'ASYNC_NOT_ALLOWED' });

// ─── Forbidden identifiers ──────────────────────────────────────────────────

const FORBIDDEN = [
  'eval', 'Function', 'setTimeout', 'setInterval', 'setImmediate',
  'queueMicrotask', 'fetch', 'XMLHttpRequest', 'WebSocket',
  'document', 'window', 'navigator',
  'localStorage', 'sessionStorage', 'indexedDB',
  'OffscreenCanvas', 'HTMLCanvasElement',
  'crypto', 'Date', 'performance',
  'Proxy', 'Reflect',
  'WeakRef', 'FinalizationRegistry',
  'SharedArrayBuffer', 'Atomics',
  'Worker',
  'process', 'module', 'global', 'globalThis', 'self', 'require',
];

for (const id of FORBIDDEN) {
  test(`forbidden: ${id}`,
    `export default function(THREE) { const x = ${id}; return new THREE.Group(); }`,
    { rule: 'FORBIDDEN_IDENTIFIER', detail: new RegExp(id) });
}

test('forbidden: naming a local variable "fetch"',
  `export default function(THREE) { const fetch = 1; return new THREE.Group(); }`,
  { rule: 'FORBIDDEN_IDENTIFIER', detail: /fetch/ });

// ─── Math.random ─────────────────────────────────────────────────────────────

test('Math.random',
  `export default function(THREE) { const r = Math.random(); return new THREE.Group(); }`,
  { rule: 'FORBIDDEN_IDENTIFIER', detail: /Math\.random/ });

// ─── Dynamic import / import.meta ────────────────────────────────────────────

test('dynamic import()',
  `export default function(THREE) { import("data:text/javascript,1"); return new THREE.Group(); }`,
  { rule: 'FORBIDDEN_IDENTIFIER', detail: /dynamic import/ });

test('dynamic import() with variable',
  `export default function(THREE) { const url = "data:text/javascript,1"; import(url); return new THREE.Group(); }`,
  { rule: 'FORBIDDEN_IDENTIFIER', detail: /dynamic import/ });

test('import.meta',
  `export default function(THREE) { const u = import.meta.url; return new THREE.Group(); }`,
  { rule: 'FORBIDDEN_IDENTIFIER', detail: /import\.meta/ });

test('static import statement',
  `import fs from "fs";
  export default function(THREE) { return new THREE.Group(); }`,
  { rule: 'FORBIDDEN_IDENTIFIER', detail: /import statement/ });

// ─── Forbidden Three.js APIs ─────────────────────────────────────────────────

const FORBIDDEN_THREE = [
  'AnimationMixer', 'SkinnedMesh', 'Bone', 'Skeleton',
  'MeshLambertMaterial', 'MeshPhongMaterial',
  'ShaderMaterial', 'RawShaderMaterial',
  'GLTFLoader', 'TextureLoader', 'FileLoader', 'ImageLoader',
  'CanvasTexture', 'VideoTexture', 'CompressedTexture', 'CubeTexture',
  'AnimationClip', 'KeyframeTrack',
];

for (const api of FORBIDDEN_THREE) {
  test(`forbidden THREE.${api}`,
    `export default function(THREE) { new THREE.${api}(); return new THREE.Group(); }`,
    { rule: 'FORBIDDEN_THREE_API', detail: new RegExp(api) });
}

test('THREE.MathUtils.seededRandom',
  `export default function(THREE) { THREE.MathUtils.seededRandom(); return new THREE.Group(); }`,
  { rule: 'FORBIDDEN_THREE_API', detail: /seededRandom/ });

test('THREE.MathUtils.generateUUID',
  `export default function(THREE) { THREE.MathUtils.generateUUID(); return new THREE.Group(); }`,
  { rule: 'FORBIDDEN_THREE_API', detail: /generateUUID/ });

// ─── Destructure / alias / spread bypasses ──────────────────────────────────
//
// Closing loopholes where an analyzer that only inspects `THREE.X`
// MemberExpressions would miss:
//
//   const { ShaderMaterial } = THREE;
//   const X = THREE; new X.ShaderMaterial();
//   const { ...rest } = THREE;
//   const obj = { ...THREE };

test('destructure forbidden THREE member',
  `export default function(THREE) {
    const { ShaderMaterial } = THREE;
    return new THREE.Group();
  }`,
  { rule: 'FORBIDDEN_THREE_API', detail: /ShaderMaterial \(destructured\)/ });

test('destructure unknown THREE member',
  `export default function(THREE) {
    const { TorusKnotCurve } = THREE;
    return new THREE.Group();
  }`,
  { rule: 'UNKNOWN_THREE_API', detail: /TorusKnotCurve/ });

test('destructure with alias (forbidden)',
  `export default function(THREE) {
    const { TextureLoader: TL } = THREE;
    return new THREE.Group();
  }`,
  { rule: 'FORBIDDEN_THREE_API', detail: /TextureLoader/ });

test('destructure multi (one forbidden)',
  `export default function(THREE) {
    const { Group, CanvasTexture } = THREE;
    return new Group();
  }`,
  { rule: 'FORBIDDEN_THREE_API', detail: /CanvasTexture/ });

test('destructure nested MathUtils.seededRandom',
  `export default function(THREE) {
    const { MathUtils: { seededRandom } } = THREE;
    return new THREE.Group();
  }`,
  { rule: 'FORBIDDEN_THREE_API', detail: /seededRandom \(destructured\)/ });

test('destructure only allowlisted (OK)',
  `export default function(THREE) {
    const { Mesh, Group, BoxGeometry, MeshStandardMaterial } = THREE;
    const m = new Mesh(new BoxGeometry(0.1,0.1,0.1), new MeshStandardMaterial());
    return new Group().add(m);
  }`,
  'pass');

test('rest-destructure of THREE',
  `export default function(THREE) {
    const { ...rest } = THREE;
    return new THREE.Group();
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN', detail: /rest-destructure/ });

test('alias THREE (const X = THREE)',
  `export default function(THREE) {
    const X = THREE;
    return new X.Group();
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN', detail: /aliasing THREE as `X`/ });

test('reassignment alias (X = THREE)',
  `export default function(THREE) {
    let X = {};
    X = THREE;
    return new X.Group();
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN', detail: /aliasing THREE as `X`/ });

test('object-spread of THREE',
  `export default function(THREE) {
    const obj = { ...THREE };
    return new obj.Group();
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN', detail: /spread of THREE/ });

test('call-spread of THREE',
  `export default function(THREE) {
    const args = [];
    args.push(...THREE);
    return new THREE.Group();
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN', detail: /spread of THREE/ });

test('array-destructure of THREE',
  `export default function(THREE) {
    const [a, b] = THREE;
    return new THREE.Group();
  }`,
  { rule: 'THREE_ALIAS_FORBIDDEN', detail: /array-destructuring/ });

// ─── Unknown THREE members (hallucinated) ────────────────────────────────────

// Names that are NOT real THREE members should produce UNKNOWN_THREE_API, not
// FORBIDDEN_THREE_API — the latter implies "real API, deliberately excluded".
const UNKNOWN_THREE = [
  'TorusKnotCurve',         // real TorusKnotGeometry exists, curve variant does not
  'PyramidGeometry',        // plausible-sounding hallucination
  'MagicPhysicsMaterial',   // pure fiction
];

for (const api of UNKNOWN_THREE) {
  test(`unknown THREE.${api}`,
    `export default function(THREE) { new THREE.${api}(); return new THREE.Group(); }`,
    { rule: 'UNKNOWN_THREE_API', detail: new RegExp(api) });
}

// ─── Newly-allowed THREE members (regression fence) ──────────────────────────

// These were added to the allowlist — confirm they now pass static analysis.
const NEWLY_ALLOWED = [
  'CapsuleGeometry',
  'EllipseCurve', 'ArcCurve', 'LineCurve', 'SplineCurve',
  'QuadraticBezierCurve', 'CubicBezierCurve',
  'Spherical', 'Cylindrical', 'Triangle', 'Line3', 'Box2',
  'RGFormat', 'HalfFloatType', 'LuminanceFormat',
  'StaticDrawUsage', 'DynamicDrawUsage', 'StreamDrawUsage',
];

for (const api of NEWLY_ALLOWED) {
  test(`allowed THREE.${api} (static analysis)`,
    `export default function(THREE) { const x = THREE.${api}; return new THREE.Group(); }`,
    'pass');
}

// ─── THREE at top level ──────────────────────────────────────────────────────

test('THREE referenced at top level',
  `const x = THREE;
  export default function(THREE) { return new THREE.Group(); }`,
  { rule: 'THREE_AT_TOP_LEVEL' });

// ─── Computed property access ────────────────────────────────────────────────

test('computed access on THREE',
  `export default function(THREE) { const n = "Group"; return new THREE[n](); }`,
  { rule: 'COMPUTED_PROPERTY_ACCESS', detail: /THREE/ });

test('computed access on Math',
  `export default function(THREE) { const fn = "sin"; Math[fn](1); return new THREE.Group(); }`,
  { rule: 'COMPUTED_PROPERTY_ACCESS', detail: /Math/ });

test('computed access on Object',
  `export default function(THREE) { Object["keys"]({}); return new THREE.Group(); }`,
  { rule: 'COMPUTED_PROPERTY_ACCESS', detail: /Object/ });

// ─── Identifier not on allowlist ─────────────────────────────────────────────

test('unknown global',
  `export default function(THREE) { const x = someUnknownGlobal; return new THREE.Group(); }`,
  { rule: 'IDENTIFIER_NOT_ALLOWED', detail: /someUnknownGlobal/ });

test('console (not in allowlist)',
  `export default function(THREE) { console.log("hi"); return new THREE.Group(); }`,
  { rule: 'IDENTIFIER_NOT_ALLOWED', detail: /console/ });

test('XMLSerializer (not in allowlist)',
  `export default function(THREE) { new XMLSerializer(); return new THREE.Group(); }`,
  { rule: 'IDENTIFIER_NOT_ALLOWED', detail: /XMLSerializer/ });

// ─── File size exceeded ──────────────────────────────────────────────────────

test('file size > 1 MB',
  `export default function(THREE) { return new THREE.Group(); }` + ' '.repeat(1024 * 1024),
  { rule: 'FILE_SIZE_EXCEEDED' });

// ─── Parse error ─────────────────────────────────────────────────────────────

test('syntax error',
  `export default function(THREE { return new THREE.Group(); }`,
  { rule: 'PARSE_ERROR' });

test('completely invalid source',
  `}{][!!@@##$$`,
  { rule: 'PARSE_ERROR' });

// ─── Literal budget exceeded ─────────────────────────────────────────────────

test('literal budget > 50 KB (string)',
  `export default function(THREE) {
    const s = "${'x'.repeat(51 * 1024)}";
    return new THREE.Group();
  }`,
  { rule: 'LITERAL_BUDGET_EXCEEDED' });

test('literal budget > 50 KB (numeric array)',
  `export default function(THREE) {
    const a = [${Array(13000).fill('3.14159').join(',')}];
    return new THREE.Group();
  }`,
  { rule: 'LITERAL_BUDGET_EXCEEDED' });

// Codegen / constructor bypass

test('array.filter.constructor (Function bypass)',
  `export default function(THREE) {
    const fn = [].filter.constructor;
    fn("return this")();
    return new THREE.Group();
  }`,
  { rule: 'FORBIDDEN_PROPERTY_ACCESS', detail: /\.constructor/ });

test('string.constructor.constructor (Function bypass)',
  `export default function(THREE) {
    const F = "".constructor.constructor;
    return new THREE.Group();
  }`,
  { rule: 'FORBIDDEN_PROPERTY_ACCESS', detail: /\.constructor/ });

test('number.constructor.constructor',
  `export default function(THREE) {
    const F = (0).constructor.constructor;
    return new THREE.Group();
  }`,
  { rule: 'FORBIDDEN_PROPERTY_ACCESS', detail: /\.constructor/ });

test('__proto__ access',
  `export default function(THREE) {
    const p = {}.__proto__;
    return new THREE.Group();
  }`,
  { rule: 'FORBIDDEN_PROPERTY_ACCESS', detail: /__proto__/ });

test('__defineGetter__ access',
  `export default function(THREE) {
    const obj = {};
    obj.__defineGetter__("x", () => 1);
    return new THREE.Group();
  }`,
  { rule: 'FORBIDDEN_PROPERTY_ACCESS', detail: /__defineGetter__/ });

test('constructor via method chain',
  `export default function(THREE) {
    const evil = [].map.constructor("return globalThis")();
    return new THREE.Group();
  }`,
  { rule: 'FORBIDDEN_PROPERTY_ACCESS', detail: /\.constructor/ });

// ─── Summary ─────────────────────────────────────────────────────────────────

console.log('');
console.log(`passed: ${passed}`);
console.log(`failed: ${failed}`);
console.log(`total:  ${passed + failed}`);
process.exit(failed > 0 ? 1 : 0);
