/**
 * Validation worker thread.
 *
 * Runs in a worker_thread with resourceLimits for V8-level heap enforcement.
 * Loads Three.js once on startup (warm), then processes validation requests:
 *   1. Compile miner source via new Function() (no ESM module cache)
 *   2. Execute generate(THREE) with runtime guards
 *   3. Run post-validation checks on the returned root
 *   4. Post structured result back to parent
 *
 * Source is compiled with `new Function()` instead of `import()` to avoid
 * leaking entries in Node's ESM module cache. The server transforms the
 * source using exact AST byte offsets (export default → return) before
 * sending it here, so the worker compiles it directly under strict mode.
 *
 * Because there is no ESM loader involvement, ALL runtime traps (including
 * Function and eval) are installed in a single phase before any miner code
 * executes — no two-phase dance needed.
 */

import { parentPort } from 'node:worker_threads';
import * as THREE from 'three';
import { postValidate } from './post-validate.js';
import { createSafeThree } from './safeThree.js';

// One Proxy instance per worker — shared across all requests. The Proxy is
// stateless and its backing object only holds references to allowlisted
// THREE members (no per-request data), so reuse is safe and avoids paying
// Proxy-construction cost on every validation.
const SAFE_THREE = createSafeThree(THREE);

const SafeFunction = Function;

const TRAPPED_GLOBALS = [
  'setTimeout', 'setInterval', 'setImmediate', 'queueMicrotask',
  'fetch', 'XMLHttpRequest', 'WebSocket',
  'crypto',
  'Date', 'performance',
  'eval', 'Function',
  'process', 'global',
  'WeakRef', 'FinalizationRegistry',
  'SharedArrayBuffer', 'Atomics',
];

const CODEGEN_PROTOTYPES = [
  SafeFunction.prototype,
  Object.getPrototypeOf(function*(){}),
  Object.getPrototypeOf(async function(){}),
  Object.getPrototypeOf(async function*(){}),
];

function trapViolation(name) {
  throw new Error(`Runtime violation: ${name} is forbidden`);
}

parentPort.on('message', async (msg) => {
  if (msg.type !== 'validate') return;

  const { source, requestId } = msg;

  const origRandom = Math.random;
  const savedGlobals = new Map();
  const savedCtors = [];

  try {
    let seed = 0x12345678;
    Math.random = () => {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };

    for (const name of TRAPPED_GLOBALS) {
      if (!(name in globalThis)) continue;
      savedGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
      try {
        Object.defineProperty(globalThis, name, {
          get() { trapViolation(name); },
          configurable: true,
        });
      } catch {
        try { globalThis[name] = undefined; } catch {}
      }
    }

    for (const proto of CODEGEN_PROTOTYPES) {
      const desc = Object.getOwnPropertyDescriptor(proto, 'constructor');
      savedCtors.push({ proto, desc });
      Object.defineProperty(proto, 'constructor', {
        get() { trapViolation('Function constructor'); },
        configurable: true,
      });
    }

    const body = `'use strict';\n${source}`;
    let factory;
    try {
      factory = new SafeFunction(body);
    } catch (err) {
      parentPort.postMessage({
        type: 'result',
        requestId,
        result: {
          passed: false,
          failures: [{
            stage: 'execution',
            rule: 'EXECUTION_THREW',
            detail: `compilation failed: ${err.message}`,
          }],
          metrics: null,
        },
      });
      return;
    }

    const generate = factory();

    if (typeof generate !== 'function') {
      parentPort.postMessage({
        type: 'result',
        requestId,
        result: {
          passed: false,
          failures: [{
            stage: 'execution',
            rule: 'INVALID_RETURN_TYPE',
            detail: 'default export is not a function',
          }],
          metrics: null,
        },
      });
      return;
    }

    // Hand the miner a capability-filtered view of THREE. This is the
    // runtime security boundary: no matter how miner code routes this
    // binding, every `X.ShaderMaterial`-style access must come back through
    // the Proxy's get trap and is either allowed or rejected here. Static
    // analysis remains as a pre-flight ergonomic check.
    const root = generate(SAFE_THREE);

    if (root instanceof Promise) {
      parentPort.postMessage({
        type: 'result',
        requestId,
        result: {
          passed: false,
          failures: [{
            stage: 'execution',
            rule: 'ASYNC_NOT_ALLOWED',
            detail: 'generate() returned a Promise',
          }],
          metrics: null,
        },
      });
      return;
    }

    const result = postValidate(THREE, root);
    parentPort.postMessage({ type: 'result', requestId, result });
  } catch (err) {
    const rule = err.message?.includes('Runtime violation')
      ? 'RUNTIME_VIOLATION'
      : 'EXECUTION_THREW';
    parentPort.postMessage({
      type: 'result',
      requestId,
      result: {
        passed: false,
        failures: [{
          stage: 'execution',
          rule,
          detail: err.message || String(err),
        }],
        metrics: null,
      },
    });
  } finally {
    Math.random = origRandom;
    for (const [name, desc] of savedGlobals) {
      try { Object.defineProperty(globalThis, name, desc); }
      catch { try { globalThis[name] = desc?.value; } catch {} }
    }
    for (const { proto, desc } of savedCtors) {
      try { Object.defineProperty(proto, 'constructor', desc); }
      catch {}
    }
  }
});

parentPort.postMessage({ type: 'ready' });
