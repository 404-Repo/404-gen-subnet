/**
 * Execute the miner module via dynamic import on a temp file.
 *
 * NOTE: this is a conformance check, NOT a security sandbox. The miner is
 * running their own code on their own machine. The production validator uses
 * isolated-vm and Puppeteer; this tool only mirrors the spec's *rules*, not
 * the spec's *isolation*. The static analysis above is the same in both, so a
 * miner can trust that anything passing here will pass the production
 * validator's static stage.
 *
 * Wall-clock timeout is enforced via Promise.race. Heap cap is not enforced
 * here — Node provides no per-call heap limit. The production runtime
 * (isolated-vm) enforces both. Mention this in the CLI output.
 */

import * as THREE from 'three';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs/promises';

const TIMEOUT_MS = 5000;

export async function execute(source) {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), '404-validate-'));
  const tmpFile = path.join(tmpDir, 'generate.mjs');

  try {
    await fs.writeFile(tmpFile, source, 'utf8');

    const moduleUrl = pathToFileURL(tmpFile).href + '?t=' + Date.now();

    let module;
    try {
      module = await import(moduleUrl);
    } catch (err) {
      return {
        root: null,
        elapsedMs: 0,
        failure: {
          stage: 'module_load',
          rule: 'EXECUTION_THREW',
          detail: err.message,
        },
      };
    }

    const generate = module.default;
    if (typeof generate !== 'function') {
      return {
        root: null,
        elapsedMs: 0,
        failure: {
          stage: 'module_load',
          rule: 'INVALID_RETURN_TYPE',
          detail: 'default export is not a function',
        },
      };
    }

    const start = Date.now();
    let timer;
    let result;
    try {
      result = await Promise.race([
        Promise.resolve().then(() => generate(THREE)),
        new Promise((_, reject) => {
          timer = setTimeout(
            () => reject(new Error('__TIMEOUT__')),
            TIMEOUT_MS,
          );
        }),
      ]);
    } catch (err) {
      const elapsedMs = Date.now() - start;
      if (err.message === '__TIMEOUT__') {
        return {
          root: null,
          elapsedMs,
          failure: {
            stage: 'execution',
            rule: 'TIMEOUT_EXCEEDED',
            detail: `generate() did not return within ${TIMEOUT_MS}ms`,
          },
        };
      }
      return {
        root: null,
        elapsedMs,
        failure: {
          stage: 'execution',
          rule: 'EXECUTION_THREW',
          detail: err.message,
        },
      };
    } finally {
      if (timer) clearTimeout(timer);
    }

    const elapsedMs = Date.now() - start;

    if (result && typeof result.then === 'function') {
      return {
        root: null,
        elapsedMs,
        failure: {
          stage: 'execution',
          rule: 'ASYNC_NOT_ALLOWED',
          detail: 'generate() returned a Promise',
        },
      };
    }

    return { root: result, elapsedMs, failure: null };
  } finally {
    await fs.rm(tmpDir, { recursive: true, force: true });
  }
}
