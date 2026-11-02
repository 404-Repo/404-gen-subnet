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

export class ValidationUnavailableError extends Error {}

class ValidationPool {
  constructor() {
    this.workers = [];
    this.available = [];
    this.waiting = [];
    this._nextRequestId = 0;
    this._shuttingDown = false;
    this._replacing = 0;
  }

  async init() {
    this._shuttingDown = false;
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
          this._settle(worker, msg.requestId, { result: msg.result });
        }
      });

      worker.on('error', (err) => {
        if (!resolved) {
          resolved = true;
          reject(err);
          return;
        }
        this._retireWorker(worker, err.code === 'ERR_WORKER_OUT_OF_MEMORY'
          ? { result: {
            passed: false,
            failures: [{ stage: 'execution', rule: 'HEAP_EXCEEDED', detail: 'worker killed (exceeded 256 MB heap)' }],
            metrics: null,
          } }
          : { error: new ValidationUnavailableError('validation worker failed') });
      });

      worker.on('exit', (code) => {
        if (!resolved) {
          resolved = true;
          reject(new Error(`worker exited during init with code ${code}`));
          return;
        }
        this._retireWorker(worker, {
          error: new ValidationUnavailableError('validation worker exited unexpectedly'),
        });
        if (!this._shuttingDown) {
          this._replaceWorker(worker);
        }
      });
    });
  }

  _settle(worker, requestId, { result, error, release = true }) {
    const entry = worker._pending.get(requestId);
    if (!entry) return;
    worker._pending.delete(requestId);
    clearTimeout(entry.timer);
    if (error) {
      entry.reject(error);
    } else {
      if (release) this._release(worker);
      entry.resolve(result);
    }
  }

  _retireWorker(worker, outcome) {
    worker._failed = true;
    this.available = this.available.filter((w) => w !== worker);
    for (const requestId of worker._pending.keys()) {
      this._settle(worker, requestId, outcome);
    }
  }

  async _replaceWorker(dead) {
    const idx = this.workers.indexOf(dead);
    if (idx === -1) return;
    this.workers.splice(idx, 1);
    this._replacing++;

    try {
      const fresh = await this._createWorker();
      if (this._shuttingDown) {
        await fresh.terminate();
        return;
      }
      this.workers.push(fresh);

      if (this.waiting.length > 0) {
        const next = this.waiting.shift();
        next.resolve(fresh);
      } else {
        this.available.push(fresh);
      }
    } catch (err) {
      console.error('[validation-pool] failed to create replacement worker:', err.message);
    } finally {
      this._replacing--;
      if (this._replacing === 0 && !this.workers.some((w) => !w._failed)) {
        for (const next of this.waiting.splice(0)) {
          next.reject(new ValidationUnavailableError('no validation workers available'));
        }
      }
    }
  }

  async validate(source) {
    const worker = await this._acquire();
    const requestId = this._nextRequestId++;

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._settle(worker, requestId, { result: {
          passed: false,
          failures: [{ stage: 'execution', rule: 'TIMEOUT_EXCEEDED', detail: `exceeded ${TIMEOUT_MS}ms` }],
          metrics: null,
        }, release: false });
        worker.terminate().catch(() => {});
      }, TIMEOUT_MS);

      worker._pending.set(requestId, { resolve, reject, timer });

      try {
        worker.postMessage({ type: 'validate', source, requestId });
      } catch {
        this._retireWorker(worker, {
          error: new ValidationUnavailableError('could not dispatch validation'),
        });
        worker.terminate().catch(() => {});
      }
    });
  }

  _acquire() {
    if (this._shuttingDown || (!this.workers.some((w) => !w._failed) && this._replacing === 0)) {
      return Promise.reject(new ValidationUnavailableError('no validation workers available'));
    }
    if (this.available.length > 0) {
      return Promise.resolve(this.available.shift());
    }
    return new Promise((resolve, reject) => {
      this.waiting.push({ resolve, reject });
    });
  }

  _release(worker) {
    if (worker._failed || !this.workers.includes(worker)) return;
    if (this.waiting.length > 0) {
      this.waiting.shift().resolve(worker);
    } else {
      this.available.push(worker);
    }
  }

  async shutdown() {
    this._shuttingDown = true;
    for (const next of this.waiting.splice(0)) {
      next.reject(new ValidationUnavailableError('validation pool is shutting down'));
    }
    for (const w of this.workers) {
      await w.terminate().catch(() => {});
    }
    this.workers = [];
    this.available = [];
    this.waiting = [];
  }
}

export const validationPool = new ValidationPool();
