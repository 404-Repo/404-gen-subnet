/**
 * Worker-thread runner for miner code.
 *
 * This file is loaded inside a Node.js worker thread spawned by execute.js.
 * Everything from module load through post-validation happens here, so the
 * main thread only sees a JSON summary of the run. Advantages:
 *
 *   1. `worker.terminate()` from the main thread preempts synchronous loops,
 *      giving us real timeout enforcement instead of post-hoc checks.
 *   2. Module evaluation time counts against the budget — the main thread
 *      starts its clock *before* instantiating the worker, so any work done
 *      at module top level shows up in the total.
 *   3. `resourceLimits.maxOldGenerationSizeMb` on the Worker constructor
 *      gives us an actual heap cap. Exceeding it terminates the worker with
 *      an ERR_WORKER_OUT_OF_MEMORY error that the main thread catches and
 *      reports as HEAP_EXCEEDED.
 *
 * The worker receives the miner source as `workerData.source`, writes it to
 * a temp file (required for Node ESM dynamic import), imports it, calls the
 * default export, and runs post-validation. A single `parentPort.postMessage`
 * reports the outcome. If the worker is terminated or crashes, the main
 * thread synthesizes the appropriate failure from the exit event.
 */

import { parentPort, workerData } from 'node:worker_threads';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import * as THREE from 'three';
import { postValidate } from './postValidation.js';
import { createSafeThree } from './safeThree.js';

// Built once at module load, reused across the worker's single run.
// The Proxy is stateless — its backing object only holds references to
// allowlisted THREE members, with no per-request state — so one-shot
// construction at import time keeps the hot path free of proxy setup cost
// and avoids allocating transient proxies on every invocation.
const SAFE_THREE = createSafeThree(THREE);

async function run() {
  const { source } = workerData;

  // Write source to a tmp file so Node's ESM loader will resolve it.
  // If the worker is terminated before cleanup runs, the OS will reap /tmp.
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), '404-validate-worker-'));
  const tmpFile = path.join(tmpDir, 'generate.mjs');
  fs.writeFileSync(tmpFile, source, 'utf8');

  const moduleStart = Date.now();
  let module;
  try {
    // Cache-bust so repeated validations of the same source re-evaluate.
    const url = pathToFileURL(tmpFile).href + '?t=' + Date.now();
    module = await import(url);
  } catch (err) {
    parentPort.postMessage({
      failures: [
        {
          stage: 'module_load',
          rule: 'EXECUTION_THREW',
          detail: err && err.message ? err.message : String(err),
        },
      ],
      metrics: null,
      moduleLoadMs: Date.now() - moduleStart,
      executionMs: 0,
    });
    cleanup(tmpDir);
    return;
  }
  const moduleLoadMs = Date.now() - moduleStart;

  const generate = module.default;
  if (typeof generate !== 'function') {
    parentPort.postMessage({
      failures: [
        {
          stage: 'module_load',
          rule: 'INVALID_RETURN_TYPE',
          detail: 'default export is not a function',
        },
      ],
      metrics: null,
      moduleLoadMs,
      executionMs: 0,
    });
    cleanup(tmpDir);
    return;
  }

  // Call generate() synchronously and capture the raw return value BEFORE any
  // await. If we awaited here, a Promise return value would be unwrapped and
  // the thenable check below would never fire.
  const execStart = Date.now();
  let rawResult;
  try {
    // Runtime capability boundary. Even if miner code subverts the static
    // analyzer via some JS syntax we haven't anticipated, every `X.ShaderMaterial`
    // access flows through the Proxy's get trap and is enforced there.
    rawResult = generate(SAFE_THREE);
  } catch (err) {
    parentPort.postMessage({
      failures: [
        {
          stage: 'execution',
          rule: 'EXECUTION_THREW',
          detail: err && err.message ? err.message : String(err),
        },
      ],
      metrics: null,
      moduleLoadMs,
      executionMs: Date.now() - execStart,
    });
    cleanup(tmpDir);
    return;
  }
  const executionMs = Date.now() - execStart;

  // Reject any thenable — real Promise, duck-typed, or chained.
  if (
    rawResult !== null &&
    (typeof rawResult === 'object' || typeof rawResult === 'function') &&
    typeof rawResult.then === 'function'
  ) {
    parentPort.postMessage({
      failures: [
        {
          stage: 'execution',
          rule: 'ASYNC_NOT_ALLOWED',
          detail: 'generate() returned a Promise or thenable',
        },
      ],
      metrics: null,
      moduleLoadMs,
      executionMs,
    });
    cleanup(tmpDir);
    return;
  }

  // Post-validation runs inside the worker too, so the main thread never
  // handles miner-constructed Three.js objects directly. This mirrors the
  // production split where miner code lives entirely inside the sandbox.
  let postResult;
  try {
    postResult = postValidate(rawResult);
  } catch (err) {
    parentPort.postMessage({
      failures: [
        {
          stage: 'post_validation',
          rule: 'EXECUTION_THREW',
          detail: err && err.message ? err.message : String(err),
        },
      ],
      metrics: null,
      moduleLoadMs,
      executionMs,
    });
    cleanup(tmpDir);
    return;
  }

  parentPort.postMessage({
    failures: postResult.failures,
    metrics: postResult.metrics,
    moduleLoadMs,
    executionMs,
  });

  cleanup(tmpDir);
}

function cleanup(tmpDir) {
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    // Best-effort: if we can't clean up (permissions, weird state), let it go.
  }
}

run().catch((err) => {
  // Any unhandled error in the runner itself gets reported as an execution
  // failure so the main thread always has something to work with.
  parentPort.postMessage({
    failures: [
      {
        stage: 'execution',
        rule: 'EXECUTION_THREW',
        detail: err && err.stack ? err.stack : String(err),
      },
    ],
    metrics: null,
    moduleLoadMs: 0,
    executionMs: 0,
  });
});
