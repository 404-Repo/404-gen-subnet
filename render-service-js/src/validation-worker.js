/**
 * Validation worker thread.
 *
 * Runs in a worker_thread with resourceLimits for V8-level heap enforcement.
 * Loads Three.js once on startup (warm), then processes validation requests:
 *   1. Import miner source via data: URI
 *   2. Execute generate(THREE) with runtime guards
 *   3. Run post-validation checks on the returned root
 *   4. Post structured result back to parent
 *
 * Runtime hardening is applied in two phases:
 *   Phase 1 (before import): locks Function.prototype.constructor on all
 *     function prototypes (blocks codegen via .constructor chains) and traps
 *     all Node.js-available forbidden globals that don't interfere with ESM
 *     module loading.
 *   Phase 2 (after import, before generate): traps Function and eval on
 *     globalThis. These must wait until after import() because Node.js's ESM
 *     loader accesses them during module compilation.
 */

import { parentPort } from 'node:worker_threads';
import * as THREE from 'three';
import { postValidate } from './post-validate.js';

const TRAPPED_GLOBALS = [
  'setTimeout', 'setInterval', 'setImmediate', 'queueMicrotask',
  'fetch', 'XMLHttpRequest', 'WebSocket',
  'crypto',
  'Date', 'performance',
  'process', 'global',
  'WeakRef', 'FinalizationRegistry',
  'SharedArrayBuffer', 'Atomics',
];

const POST_IMPORT_TRAPS = ['eval', 'Function'];

const CODEGEN_PROTOTYPES = [
  Function.prototype,
  Object.getPrototypeOf(function*(){}),
  Object.getPrototypeOf(async function(){}),
  Object.getPrototypeOf(async function*(){}),
];

let importCounter = 0;

function trapViolation(name) {
  throw new Error(`Runtime violation: ${name} is forbidden`);
}

parentPort.on('message', async (msg) => {
  if (msg.type !== 'validate') return;

  const { source, requestId } = msg;

  const origRandom = Math.random;
  const savedGlobals = new Map();
  const savedPostImport = new Map();
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

    const tag = `\n/* __v${importCounter++}__ */`;
    const encoded = Buffer.from(source + tag).toString('base64');
    const mod = await import(`data:text/javascript;base64,${encoded}`);

    for (const name of POST_IMPORT_TRAPS) {
      if (!(name in globalThis)) continue;
      savedPostImport.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
      try {
        Object.defineProperty(globalThis, name, {
          get() { trapViolation(name); },
          configurable: true,
        });
      } catch {
        try { globalThis[name] = undefined; } catch {}
      }
    }

    if (typeof mod.default !== 'function') {
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

    const root = mod.default(THREE);

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
    for (const [name, desc] of savedPostImport) {
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
