/**
 * Adversarial probe suite — every case here encodes a pattern an attacker
 * might try to route a forbidden Three.js API through the static analyzer.
 *
 * The discipline this file enforces:
 *   1. Any PR that touches `src/staticAnalysis.js` MUST add at least one
 *      case here (new attack pattern, or new variant of an old one).
 *   2. Cases marked `expect: 'reject'` must reject with *some* rule. We do
 *      not pin the exact rule, because the right rule may shift as the
 *      analyzer evolves — only that the analyzer catches it.
 *   3. Cases marked `expect: 'pass'` are legitimate patterns that must keep
 *      working. A regression here means the analyzer became too strict.
 *
 * This file exists because two real bypasses (`this.t = THREE`,
 * `obj.t = THREE`) shipped through two rounds of "we closed the holes"
 * review before this probe suite caught them.
 *
 * Run:  node test/run-adversary-probes.mjs
 *       npm test   (wired into the test script)
 */

import { parseSource } from '../src/parse.js';
import { staticAnalyze } from '../src/staticAnalysis.js';

function analyze(source) {
  const { ast, failures: parseFailures } = parseSource(source);
  if (parseFailures.length > 0) {
    return { passed: false, failures: parseFailures };
  }
  const failures = staticAnalyze(ast);
  return { passed: failures.length === 0, failures };
}

const cases = [
  // ── Direct reference-position bypasses (parent-context rule) ──────────────

  { name: 'tagged-template-launder', expect: 'reject', src: `
    export default function(THREE) {
      const tag = (strings, ...values) => values[0];
      const X = tag\`$\${THREE}\`;
      return new X.ShaderMaterial({});
    }` },

  { name: 'for-of-over-array-literal', expect: 'reject', src: `
    export default function(THREE) {
      for (const X of [THREE]) {
        return new X.ShaderMaterial({});
      }
    }` },

  { name: 'throw-and-catch-launder', expect: 'reject', src: `
    export default function(THREE) {
      try { throw THREE; } catch (X) { return new X.ShaderMaterial({}); }
    }` },

  { name: 'logical-or-launder', expect: 'reject', src: `
    export default function(THREE) {
      const X = THREE || {};
      return new X.ShaderMaterial({});
    }` },

  { name: 'ternary-launder', expect: 'reject', src: `
    export default function(THREE) {
      const X = true ? THREE : null;
      return new X.ShaderMaterial({});
    }` },

  { name: 'sequence-expression-launder', expect: 'reject', src: `
    export default function(THREE) {
      const X = (0, THREE);
      return new X.ShaderMaterial({});
    }` },

  { name: 'class-field-initializer', expect: 'reject', src: `
    export default function(THREE) {
      class H { three = THREE; build() { return new this.three.ShaderMaterial({}); } }
      return new H().build();
    }` },

  { name: 'generator-yield-launder', expect: 'reject', src: `
    export default function(THREE) {
      function* gen() { yield THREE; }
      const X = gen().next().value;
      return new X.ShaderMaterial({});
    }` },

  // ── Assignment-target bypasses (LHS-shape rule) ────────────────────────────

  { name: 'stash-on-this', expect: 'reject', src: `
    class B {
      constructor(THREE) { this.t = THREE; }
      build() { return new this.t.ShaderMaterial({}); }
    }
    export default function(THREE) { return new B(THREE).build(); }` },

  { name: 'stash-on-plain-object', expect: 'reject', src: `
    export default function(THREE) {
      const obj = { t: null };
      obj.t = THREE;
      return new obj.t.ShaderMaterial({});
    }` },

  { name: 'destructure-into-member-target', expect: 'reject', src: `
    export default function(THREE) {
      const obj = {};
      ({ ShaderMaterial: obj.sm } = THREE);
      return new obj.sm({});
    }` },

  { name: 'array-destructure-with-default-launder', expect: 'reject', src: `
    export default function(THREE) {
      const obj = {};
      [obj.sm = THREE.ShaderMaterial] = [];
      return new obj.sm({});
    }` },

  // ── Call-forwarding bypasses (callee-param rule) ──────────────────────────

  { name: 'helper-param-destructured', expect: 'reject', src: `
    export default function(THREE) {
      const use = ({ ShaderMaterial, Mesh, BoxGeometry }) =>
        new Mesh(new BoxGeometry(1,1,1), new ShaderMaterial({}));
      return use(THREE);
    }` },

  { name: 'helper-param-renamed', expect: 'reject', src: `
    function make(t) {
      return new t.Mesh(new t.BoxGeometry(1,1,1), new t.MeshStandardMaterial());
    }
    export default function(THREE) { return make(THREE); }` },

  { name: 'helper-rest-param-capture', expect: 'reject', src: `
    function h(...args) {
      const t = args[0];
      return new t.Mesh(new t.BoxGeometry(1,1,1), new t.MeshStandardMaterial());
    }
    export default function(THREE) { return h(THREE); }` },

  { name: 'default-param-is-three', expect: 'reject', src: `
    export default function(THREE) {
      const h = (t = THREE) =>
        new t.Mesh(new t.BoxGeometry(1,1,1), new t.MeshStandardMaterial());
      return h();
    }` },

  { name: 'iife-destructured-param', expect: 'reject', src: `
    export default function(THREE) {
      return (({ Mesh, BoxGeometry, ShaderMaterial }) =>
        new Mesh(new BoxGeometry(1,1,1), new ShaderMaterial({})))(THREE);
    }` },

  { name: 'class-ctor-renamed-param', expect: 'reject', src: `
    class B {
      constructor(T) { this.T = T; }
      build() { return new this.T.Group(); }
    }
    export default function(THREE) { return new B(THREE).build(); }` },

  { name: 'method-call-with-three-arg', expect: 'reject', src: `
    export default function(THREE) {
      function use(t) {
        return new t.Mesh(new t.BoxGeometry(1,1,1), new t.MeshStandardMaterial());
      }
      return use.call(null, THREE);
    }` },

  // ── Module-boundary / reflective bypasses ─────────────────────────────────

  { name: 'direct-import-star', expect: 'reject', src: `
    import * as X from 'three';
    export default function(THREE) { return new X.ShaderMaterial({}); }` },

  { name: 'named-import', expect: 'reject', src: `
    import { ShaderMaterial } from 'three';
    export default function(THREE) { return new ShaderMaterial({}); }` },

  { name: 'default-import', expect: 'reject', src: `
    import T from 'three';
    export default function(THREE) { return new T.ShaderMaterial({}); }` },

  { name: 'dynamic-import', expect: 'reject', src: `
    export default function(THREE) {
      return import('three').then(m => new m.ShaderMaterial({}));
    }` },

  { name: 'commonjs-require', expect: 'reject', src: `
    export default function(THREE) {
      const X = require('three');
      return new X.ShaderMaterial({});
    }` },

  { name: 'eval-call', expect: 'reject', src: `
    export default function(THREE) {
      return eval("new THREE.ShaderMaterial({})");
    }` },

  { name: 'function-constructor', expect: 'reject', src: `
    export default function(THREE) {
      const f = new Function("THREE", "return new THREE.ShaderMaterial({})");
      return f(THREE);
    }` },

  { name: 'globalThis-lookup', expect: 'reject', src: `
    export default function(THREE) {
      return new globalThis.THREE.ShaderMaterial({});
    }` },

  { name: 'computed-property-access', expect: 'reject', src: `
    export default function(THREE) {
      const k = 'ShaderMaterial';
      return new THREE[k]({});
    }` },

  { name: 'object-assign-laundering', expect: 'reject', src: `
    export default function(THREE) {
      const target = {};
      Object.assign(target, THREE);
      return new target.ShaderMaterial({});
    }` },

  { name: 'object-values-spread', expect: 'reject', src: `
    export default function(THREE) {
      const arr = [...Object.values(THREE)];
      return new arr[0]({});
    }` },

  // ── Legitimate patterns that must keep working ────────────────────────────

  { name: 'legit-minimal-mesh', expect: 'pass', src: `
    export default function(THREE) {
      return new THREE.Mesh(new THREE.BoxGeometry(1,1,1), new THREE.MeshStandardMaterial());
    }` },

  { name: 'legit-named-destructure', expect: 'pass', src: `
    export default function(THREE) {
      const { Mesh, BoxGeometry, MeshStandardMaterial, Group } = THREE;
      const g = new Group();
      g.add(new Mesh(new BoxGeometry(1,1,1), new MeshStandardMaterial()));
      return g;
    }` },

  { name: 'legit-helper-named-THREE', expect: 'pass', src: `
    function fitToUnitCube(THREE, root) {
      const box = new THREE.Box3().setFromObject(root);
      return box.getSize(new THREE.Vector3());
    }
    export default function(THREE) {
      const g = new THREE.Group();
      fitToUnitCube(THREE, g);
      return g;
    }` },

  { name: 'legit-class-without-stashing-THREE', expect: 'pass', src: `
    class Builder {
      constructor(THREE) { this.scene = new THREE.Group(); }
      build() { return this.scene; }
    }
    export default function(THREE) { return new Builder(THREE).build(); }` },

  { name: 'legit-nested-destructure', expect: 'pass', src: `
    export default function(THREE) {
      const { MathUtils: { clamp } } = THREE;
      const x = clamp(0.5, 0, 1);
      return new THREE.Mesh(
        new THREE.BoxGeometry(x, x, x),
        new THREE.MeshStandardMaterial(),
      );
    }` },
];

let passed = 0;
let failed = 0;

for (const { name, expect, src } of cases) {
  const result = analyze(src);

  if (expect === 'pass') {
    if (result.passed) {
      passed++;
    } else {
      failed++;
      console.error(`FAIL ${name}`);
      console.error(`  expected: pass (legitimate pattern)`);
      console.error(`  got:      reject ${result.failures[0]?.rule} — ${result.failures[0]?.detail}`);
    }
    continue;
  }

  if (!result.passed) {
    passed++;
  } else {
    failed++;
    console.error(`FAIL ${name}`);
    console.error(`  expected: reject (attack pattern — possible bypass!)`);
    console.error(`  got:      pass`);
  }
}

console.log(`\npassed: ${passed}`);
console.log(`failed: ${failed}`);
console.log(`total:  ${cases.length}`);

if (failed > 0) process.exit(1);
