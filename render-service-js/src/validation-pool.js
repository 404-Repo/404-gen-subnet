/**
 * Validation worker pool.
 *
 * Manages a pool of worker_threads that execute miner code and run
 * post-validation. Each worker has V8-level heap limits (resourceLimits)
 * and the pool enforces per-request wall-clock timeouts.
 *
 * Pool size: VALIDATION_POOL_SIZE env var (default 2).
 * Timeout:   VALIDATION_TIMEOUT_MS env var (default 5000).
 */

import { Worker } from 'node:worker_threads';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKER_PATH = path.join(__dirname, 'validation-worker.js');
const POOL_SIZE = parseInt(process.env.VALIDATION_POOL_SIZE || '2', 10);
const TIMEOUT_MS = parseInt(process.env.VALIDATION_TIMEOUT_MS || '5000', 10);

class ValidationPool {
  constructor() {
    this.workers = [];
    this.available = [];
    this.waiting = [];
    this._nextRequestId = 0;
  }

  async init() {
    const promises = [];
    for (let i = 0; i < POOL_SIZE; i++) {
      promises.push(this._createWorker());
    }
    const workers = await Promise.all(promises);
    for (const w of workers) {
      this.workers.push(w);
      this.available.push(w);
    }
    console.log(`Validation pool ready (${POOL_SIZE} workers, ${TIMEOUT_MS}ms timeout)`);
  }

  _createWorker() {
    return new Promise((resolve, reject) => {
      const worker = new Worker(WORKER_PATH, {
        resourceLimits: {
          maxOldGenerationSizeMb: 256,
          maxYoungGenerationSizeMb: 64,
        },
      });

      worker._pending = new Map();
      let resolved = false;

      worker.on('message', (msg) => {
        if (msg.type === 'ready' && !resolved) {
          resolved = true;
          resolve(worker);
          return;
        }
        if (msg.type === 'result') {
          const entry = worker._pending.get(msg.requestId);
          if (entry) {
            worker._pending.delete(msg.requestId);
            clearTimeout(entry.timer);
            entry.resolve(msg.result);
          }
        }
      });

      worker.on('error', (err) => {
        if (!resolved) {
          resolved = true;
          reject(err);
          return;
        }
        const isOOM = err.code === 'ERR_WORKER_OUT_OF_MEMORY';
        const rule = isOOM ? 'HEAP_EXCEEDED' : 'EXECUTION_THREW';
        const detail = isOOM
          ? 'worker killed (exceeded 256 MB heap)'
          : err.message;
        for (const [, entry] of worker._pending) {
          clearTimeout(entry.timer);
          entry.resolve({
            passed: false,
            failures: [{ stage: 'execution', rule, detail }],
            metrics: null,
          });
        }
        worker._pending.clear();
      });

      worker.on('exit', (code) => {
        if (!resolved) {
          resolved = true;
          reject(new Error(`worker exited during init with code ${code}`));
          return;
        }
        const rule = code === null ? 'HEAP_EXCEEDED' : 'EXECUTION_THREW';
        const detail = code === null
          ? 'worker killed (likely exceeded 256 MB heap)'
          : `worker exited with code ${code}`;
        for (const [, entry] of worker._pending) {
          clearTimeout(entry.timer);
          entry.resolve({
            passed: false,
            failures: [{ stage: 'execution', rule, detail }],
            metrics: null,
          });
        }
        worker._pending.clear();
        this._replaceWorker(worker);
      });
    });
  }

  async _replaceWorker(dead) {
    const idx = this.workers.indexOf(dead);
    if (idx === -1) return;
    this.workers.splice(idx, 1);

    const wasAvailable = this.available.indexOf(dead);
    if (wasAvailable !== -1) this.available.splice(wasAvailable, 1);

    try {
      const fresh = await this._createWorker();
      this.workers.push(fresh);

      if (this.waiting.length > 0) {
        const next = this.waiting.shift();
        next(fresh);
      } else {
        this.available.push(fresh);
      }
    } catch (err) {
      console.error('[validation-pool] failed to create replacement worker:', err.message);
    }
  }

  async validate(source) {
    const worker = await this._acquire();
    const requestId = this._nextRequestId++;

    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        worker._pending.delete(requestId);
        worker.terminate().catch(() => {});
        resolve({
          passed: false,
          failures: [{ stage: 'execution', rule: 'TIMEOUT_EXCEEDED', detail: `exceeded ${TIMEOUT_MS}ms` }],
          metrics: null,
        });
      }, TIMEOUT_MS);

      worker._pending.set(requestId, { resolve: (result) => {
        this._release(worker);
        resolve(result);
      }, timer });

      worker.postMessage({ type: 'validate', source, requestId });
    });
  }

  _acquire() {
    if (this.available.length > 0) {
      return Promise.resolve(this.available.shift());
    }
    return new Promise((resolve) => {
      this.waiting.push(resolve);
    });
  }

  _release(worker) {
    if (!this.workers.includes(worker)) return;
    if (this.waiting.length > 0) {
      this.waiting.shift()(worker);
    } else {
      this.available.push(worker);
    }
  }

  async shutdown() {
    for (const w of this.workers) {
      await w.terminate().catch(() => {});
    }
    this.workers = [];
    this.available = [];
    this.waiting = [];
  }
}

export const validationPool = new ValidationPool();
