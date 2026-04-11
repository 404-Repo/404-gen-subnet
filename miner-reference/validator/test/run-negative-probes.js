#!/usr/bin/env node
/**
 * Negative probe runner.
 *
 * Each fixture in `negative-probes/` is a deliberately-broken `generate.js`
 * that MUST be rejected by the validator with a specific rule code.
 *
 * Fixtures declare the expected rule via a header comment:
 *   // @expectedRule ASYNC_NOT_ALLOWED
 *
 * The runner validates each fixture and checks:
 *   1. The validator rejects it (passed === false).
 *   2. At least one failure carries the declared rule code.
 *
 * This is the coverage that catches rule regressions — if the validator
 * silently stops rejecting a violation, this script turns red.
 *
 * Run from the validator package root:
 *   node test/run-negative-probes.js
 *
 * Exit codes:
 *   0  — every fixture was rejected with the expected rule
 *   1  — one or more fixtures behaved unexpectedly
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { validate } from '../src/index.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROBE_DIR = path.join(__dirname, 'negative-probes');

const EXPECTED_RULE_RE = /^\/\/\s*@expectedRule\s+(\S+)/m;

const probeFiles = (await fs.readdir(PROBE_DIR))
  .filter((f) => f.endsWith('.js'))
  .sort();

if (probeFiles.length === 0) {
  console.error(`No negative-probe fixtures found in ${PROBE_DIR}`);
  process.exit(1);
}

const C = process.stdout.isTTY
  ? { red: '\x1b[31m', green: '\x1b[32m', yellow: '\x1b[33m', dim: '\x1b[2m', bold: '\x1b[1m', reset: '\x1b[0m' }
  : { red: '', green: '', yellow: '', dim: '', bold: '', reset: '' };

const results = [];
let failed = 0;

for (const file of probeFiles) {
  const source = await fs.readFile(path.join(PROBE_DIR, file), 'utf8');

  const match = source.match(EXPECTED_RULE_RE);
  if (!match) {
    results.push({
      file,
      expectedRule: null,
      actualRules: [],
      status: 'NO_ANNOTATION',
      detail: 'missing // @expectedRule header',
    });
    failed++;
    continue;
  }

  const expectedRule = match[1];

  let result;
  try {
    result = await validate(source);
  } catch (err) {
    results.push({
      file,
      expectedRule,
      actualRules: [],
      status: 'ERROR',
      detail: `validate() threw: ${err.message}`,
    });
    failed++;
    continue;
  }

  const actualRules = result.failures.map((f) => f.rule);

  if (result.passed) {
    results.push({
      file,
      expectedRule,
      actualRules: [],
      status: 'UNEXPECTED_PASS',
      detail: 'validator accepted the fixture (should have rejected)',
    });
    failed++;
  } else if (actualRules.includes(expectedRule)) {
    results.push({
      file,
      expectedRule,
      actualRules,
      status: 'OK',
      detail: null,
    });
  } else {
    results.push({
      file,
      expectedRule,
      actualRules,
      status: 'WRONG_RULE',
      detail: `expected ${expectedRule}, got ${actualRules.join(', ')}`,
    });
    failed++;
  }
}

console.log('');
console.log(`${C.bold}Negative probe results${C.reset}`);
console.log('');

const cols = [
  ['fixture',        30, (r) => r.file],
  ['expected rule',  30, (r) => r.expectedRule ?? '(none)'],
  ['status',         20, (r) => statusLabel(r.status)],
];

const headerLine = cols.map(([name, w]) => name.padEnd(w)).join('');
console.log(`${C.bold}${headerLine}${C.reset}`);
console.log(C.dim + '─'.repeat(headerLine.length) + C.reset);

for (const r of results) {
  console.log(cols.map(([, w, fn]) => {
    const val = String(fn(r));
    return val.padEnd(w + extraPad(val));
  }).join(''));
}

if (failed > 0) {
  console.log('');
  console.log(`${C.red}${failed} of ${results.length} negative probes failed${C.reset}`);
  for (const r of results) {
    if (r.status === 'OK') continue;
    console.log(`  ${C.bold}${r.file}${C.reset}: ${r.detail}`);
  }
  process.exit(1);
}

console.log('');
console.log(`${C.green}all ${results.length} negative probes passed${C.reset}`);
process.exit(0);

function statusLabel(status) {
  switch (status) {
    case 'OK':              return `${C.green}OK${C.reset}`;
    case 'UNEXPECTED_PASS': return `${C.red}UNEXPECTED_PASS${C.reset}`;
    case 'WRONG_RULE':      return `${C.red}WRONG_RULE${C.reset}`;
    case 'NO_ANNOTATION':   return `${C.yellow}NO_ANNOTATION${C.reset}`;
    case 'ERROR':           return `${C.red}ERROR${C.reset}`;
    default:                return status;
  }
}

function extraPad(s) {
  const visibleLen = String(s).replace(/\x1b\[[0-9;]*m/g, '').length;
  const totalLen = String(s).length;
  return totalLen - visibleLen;
}
