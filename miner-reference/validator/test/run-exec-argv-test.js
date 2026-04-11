#!/usr/bin/env node
/**
 * Integration test for the execArgv scrubbing in execute.js.
 *
 * Spawns child Node processes with various parent launch flags that
 * previously broke the validator when inherited by the worker thread,
 * and asserts that validate() still returns PASS for examples/car.js
 * in each case.
 *
 * This test **must** run across a real process boundary. Importing
 * validate() directly into this file would exercise the validator in
 * the same process this runner is in, and `process.execArgv` would be
 * this runner's argv — not the flag combination we're trying to test.
 * The fix we're locking in is specifically about inheritance across
 * the `new Worker()` boundary, which only happens when there's a
 * parent Node process with those flags set.
 *
 * The cases below were chosen to cover:
 *
 *   1. Baseline — no parent flags. Confirms the test harness works.
 *   2. The original repro — `--input-type=module` breaks the worker
 *      unless execArgv is scrubbed.
 *   3. Flags that must be **preserved** through the scrub
 *      (`--enable-source-maps`, `--no-warnings`), paired with
 *      `--input-type=module` to prove the scrub filters selectively
 *      rather than nuking everything.
 *   4. A flag that must be **blocked** because it would silently
 *      defeat the heap cap (`--max-old-space-size`), again paired
 *      with `--input-type=module` so we know the scrub ran and the
 *      other flag survived.
 *
 * Run from the validator package root:
 *   node test/run-exec-argv-test.js
 *
 * Exit codes:
 *   0  — every case produced a PASS result in its child process
 *   1  — one or more cases failed
 */

import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = path.join(__dirname, '..');
const REPO_ROOT = path.join(PACKAGE_ROOT, '..');
const CAR_PATH = path.join(REPO_ROOT, 'examples', 'car.js');
const VALIDATOR_URL = pathToFileURL(path.join(PACKAGE_ROOT, 'src', 'index.js')).href;

// The child script. It imports validate(), runs it against car.js, and
// writes a single JSON line to stdout so this runner can parse it.
// JSON.stringify is used on the path values to survive any edge cases
// in string interpolation (e.g., Windows backslashes, spaces).
const childScript = `
import { validate } from ${JSON.stringify(VALIDATOR_URL)};
import fs from 'node:fs/promises';
const source = await fs.readFile(${JSON.stringify(CAR_PATH)}, 'utf8');
const result = await validate(source);
console.log(JSON.stringify({
  passed: result.passed,
  stagesRun: result.stagesRun,
  failures: result.failures.map((f) => ({ stage: f.stage, rule: f.rule, detail: f.detail })),
}));
`.trim();

const cases = [
  {
    name: 'baseline (no parent flags)',
    execArgs: [],
  },
  {
    name: '--input-type=module (the original repro)',
    execArgs: ['--input-type=module'],
  },
  {
    name: '--enable-source-maps --input-type=module (source maps must be preserved)',
    execArgs: ['--enable-source-maps', '--input-type=module'],
  },
  {
    name: '--no-warnings --input-type=module (no-warnings must be preserved)',
    execArgs: ['--no-warnings', '--input-type=module'],
  },
  {
    name: '--max-old-space-size=512 --input-type=module (heap flag must be blocked)',
    execArgs: ['--max-old-space-size=512', '--input-type=module'],
  },
];

const C = process.stdout.isTTY
  ? { red: '\x1b[31m', green: '\x1b[32m', dim: '\x1b[2m', bold: '\x1b[1m', reset: '\x1b[0m' }
  : { red: '', green: '', dim: '', bold: '', reset: '' };

let failed = 0;
const results = [];

for (const testCase of cases) {
  // Order matters: --input-type=module must appear before `-e` because it
  // tells Node how to interpret the subsequent -e string input.
  const args = [...testCase.execArgs, '-e', childScript];

  const proc = spawnSync('node', args, {
    encoding: 'utf8',
    cwd: REPO_ROOT,
    // Give each child up to 30 seconds. Worker startup + Three.js import
    // + validator pipeline is typically <3s, but CI environments can be slow.
    timeout: 30_000,
  });

  if (proc.error) {
    results.push({ ...testCase, status: 'SPAWN_ERROR', detail: proc.error.message });
    failed++;
    continue;
  }

  if (proc.status !== 0) {
    results.push({
      ...testCase,
      status: 'CHILD_EXIT_NONZERO',
      detail: `exit code ${proc.status}: ${(proc.stderr || '').trim().slice(0, 300)}`,
    });
    failed++;
    continue;
  }

  // Parse the last line of stdout — that's our JSON result. Earlier lines
  // (if any) would be unrelated log output.
  const stdout = (proc.stdout || '').trim();
  const jsonLine = stdout.split('\n').pop();
  let parsed;
  try {
    parsed = JSON.parse(jsonLine);
  } catch {
    results.push({
      ...testCase,
      status: 'PARSE_ERROR',
      detail: `could not parse child stdout as JSON: ${stdout.slice(0, 300)}`,
    });
    failed++;
    continue;
  }

  if (!parsed.passed) {
    const failureSummary = (parsed.failures || [])
      .map((f) => `${f.stage}/${f.rule}: ${f.detail}`)
      .join('; ');
    results.push({
      ...testCase,
      status: 'VALIDATOR_FAILED',
      detail: failureSummary || 'passed=false with no failures listed',
    });
    failed++;
    continue;
  }

  results.push({ ...testCase, status: 'OK', detail: null });
}

console.log('');
console.log(`${C.bold}execArgv inheritance tests${C.reset}`);
console.log(
  `${C.dim}Validates examples/car.js from a child Node process invoked with${C.reset}`,
);
console.log(
  `${C.dim}various parent launch flags that must not break the worker.${C.reset}`,
);
console.log('');

for (const r of results) {
  const marker =
    r.status === 'OK' ? `${C.green}OK${C.reset}       ` : `${C.red}${r.status.padEnd(16)}${C.reset} `;
  console.log(`  ${marker} ${r.name}`);
  if (r.status !== 'OK' && r.detail) {
    // Indent the failure detail for readability
    for (const line of r.detail.split('\n')) {
      console.log(`            ${C.dim}${line}${C.reset}`);
    }
  }
}

console.log('');
if (failed > 0) {
  console.log(`${C.red}${failed} of ${results.length} execArgv cases failed${C.reset}`);
  process.exit(1);
}
console.log(`${C.green}all ${results.length} execArgv cases passed${C.reset}`);
process.exit(0);
