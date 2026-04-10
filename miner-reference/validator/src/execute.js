/**
 * Run miner code inside a Node.js worker thread.
 *
 * The worker handles module load, the synchronous call to `generate(THREE)`,
 * the thenable check, and post-validation. The main thread's only job is to
 * start the clock, spawn the worker, wait for a message (or a timeout, or a
 * crash), and resolve with a normalized result.
 *
 * ### Fidelity gaps closed by using a worker
 *
 *  1. **Preemptive timeout.** `worker.terminate()` kills the worker thread
 *     immediately, even in the middle of a synchronous CPU-bound loop. The
 *     previous Promise.race approach could not preempt sync code; this one
 *     can. Matches the behavior of production `isolated-vm`.
 *
 *  2. **Module load counted against the budget.** The wall-clock timer in
 *     the main thread starts *before* worker construction, so any work done
 *     at the miner module's top level (class declarations, constant initial-
 *     izers, eager computation) eats the 5-second budget. Matches Runtime
 *     Spec § Combined Budget.
 *
 *  3. **Real heap cap.** `resourceLimits.maxOldGenerationSizeMb` tells V8 to
 *     cap the worker's old generation heap. A miner allocating 1 GB of
 *     Float32Array buffers trips ERR_WORKER_OUT_OF_MEMORY and we report
 *     HEAP_EXCEEDED. First time we can enforce this locally; previously it
 *     was only enforceable in the production runtime.
 *
 * ### Fidelity gaps still open
 *
 *  - V8 young-generation GC pauses and JIT warmup cost a few hundred ms of
 *    worker startup overhead. Real miner execution will be slightly faster
 *    in production than here.
 *  - `isolated-vm` runs in its own isolate with no shared built-ins. Node
 *    workers share some built-ins with the host (e.g. `console`). The static
 *    analysis pass already rejects forbidden identifiers, so this is not a
 *    correctness gap, just an isolation gap.
 */

import { Worker } from 'node:worker_threads';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKER_PATH = path.join(__dirname, 'worker-runner.mjs');

const TIMEOUT_MS = 5000;
const HEAP_LIMIT_MB = 256;

/**
 * @param {string} source - the miner module source text
 * @returns {Promise<{
 *   failures: Array<{ stage: string, rule: string, detail: string }>,
 *   metrics: object | null,
 *   moduleLoadMs: number | null,
 *   executionMs: number | null,
 *   totalMs: number,
 * }>}
 */
export async function execute(source) {
  return new Promise((resolve) => {
    const start = Date.now();
    let settled = false;

    const worker = new Worker(WORKER_PATH, {
      workerData: { source },
      resourceLimits: {
        maxOldGenerationSizeMb: HEAP_LIMIT_MB,
        // young-gen kept at Node's default; old-gen dominates long-lived
        // allocations like BufferGeometry attribute arrays.
      },
    });

    const settle = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      worker.terminate().catch(() => {});
      resolve({
        ...result,
        totalMs: Date.now() - start,
      });
    };

    // Real preemptive timeout: fires after TIMEOUT_MS regardless of what the
    // worker is doing. Synchronous loops, hanging promises, stuck module
    // evaluation — all caught by terminate().
    const timer = setTimeout(() => {
      settle({
        failures: [
          {
            stage: 'execution',
            rule: 'TIMEOUT_EXCEEDED',
            detail:
              `combined module-load + generate() exceeded ${TIMEOUT_MS}ms ` +
              `— worker terminated`,
          },
        ],
        metrics: null,
        moduleLoadMs: null,
        executionMs: null,
      });
    }, TIMEOUT_MS);

    worker.on('message', (msg) => {
      settle(msg);
    });

    worker.on('error', (err) => {
      // Heap cap is reported as an Error with code ERR_WORKER_OUT_OF_MEMORY
      // (or similar). Map it to HEAP_EXCEEDED so miners see the right rule.
      const isOom =
        (err && err.code && String(err.code).includes('OUT_OF_MEMORY')) ||
        (err && err.message && /out of memory/i.test(err.message));

      settle({
        failures: [
          isOom
            ? {
                stage: 'execution',
                rule: 'HEAP_EXCEEDED',
                detail: `worker exceeded ${HEAP_LIMIT_MB} MB heap cap`,
              }
            : {
                stage: 'execution',
                rule: 'EXECUTION_THREW',
                detail: err && err.message ? err.message : String(err),
              },
        ],
        metrics: null,
        moduleLoadMs: null,
        executionMs: null,
      });
    });

    worker.on('exit', (code) => {
      if (settled) return;
      // Worker exited without posting a message and without firing 'error'.
      // This shouldn't normally happen, but handle it so the caller never
      // waits forever.
      settle({
        failures: [
          {
            stage: 'execution',
            rule: 'EXECUTION_THREW',
            detail: `worker exited unexpectedly with code ${code}`,
          },
        ],
        metrics: null,
        moduleLoadMs: null,
        executionMs: null,
      });
    });
  });
}
