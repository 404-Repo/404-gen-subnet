#!/usr/bin/env node
/**
 * Budget probe runner.
 *
 * Each fixture in `budget-probes/` is a hand-written `generate.js` of a
 * different complexity class. They serve two purposes:
 *
 *   1. **CI gate.** All probes must validate cleanly. If a spec or validator
 *      change starts rejecting one of them, this script fails the build, so
 *      the impact is visible before merge.
 *
 *   2. **Headroom snapshot.** The summary table prints how much of each cap
 *      every probe uses, so you can see where realistic outputs sit relative
 *      to the limits and tune the caps with evidence.
 *
 * Run from the validator package root:
 *   node test/run-budget-probes.js
 *
 * Exit codes:
 *   0  — every probe validated cleanly
 *   1  — one or more probes failed
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { validate } from '../src/index.js';
import { LIMITS } from '../src/postValidation.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROBE_DIR = path.join(__dirname, 'budget-probes');

const probeFiles = (await fs.readdir(PROBE_DIR))
  .filter((f) => f.endsWith('.js'))
  .sort();

if (probeFiles.length === 0) {
  console.error(`No probe fixtures found in ${PROBE_DIR}`);
  process.exit(1);
}

const C = process.stdout.isTTY
  ? { red: '\x1b[31m', green: '\x1b[32m', dim: '\x1b[2m', bold: '\x1b[1m', reset: '\x1b[0m' }
  : { red: '', green: '', dim: '', bold: '', reset: '' };

const results = [];
let failed = 0;

for (const file of probeFiles) {
  const source = await fs.readFile(path.join(PROBE_DIR, file), 'utf8');
  const result = await validate(source);
  results.push({ file, result });
  if (!result.passed) failed++;
}

// Pretty summary table
console.log('');
console.log(`${C.bold}Budget probe results${C.reset}`);
console.log('');

const cols = [
  ['fixture',   24, (r) => r.file],
  ['verts',      9, (r) => pct(r.result.metrics?.vertices, LIMITS.vertices)],
  ['draws',      8, (r) => pct(r.result.metrics?.drawCalls, LIMITS.drawCalls)],
  ['depth',      8, (r) => pct(r.result.metrics?.maxDepth, LIMITS.depth)],
  ['inst',       9, (r) => pct(r.result.metrics?.instances, LIMITS.instances)],
  ['tex',        9, (r) => pct(r.result.metrics?.textureBytes, LIMITS.textureBytes)],
  ['ms',         6, (r) => `${r.result.executionMs}`],
  ['status',     8, (r) => r.result.passed ? `${C.green}PASS${C.reset}` : `${C.red}FAIL${C.reset}`],
];

const headerLine = cols.map(([name, w]) => name.padEnd(w)).join('');
console.log(`${C.bold}${headerLine}${C.reset}`);
console.log(C.dim + '─'.repeat(headerLine.length) + C.reset);

for (const r of results) {
  console.log(cols.map(([, w, fn]) => String(fn(r)).padEnd(w + extraPad(fn(r)))).join(''));
}

console.log('');
console.log(
  `${C.dim}caps: ${LIMITS.vertices.toLocaleString()} verts · ${LIMITS.drawCalls} draws · ` +
    `depth ${LIMITS.depth} · ${LIMITS.instances.toLocaleString()} inst · ` +
    `${(LIMITS.textureBytes / 1024).toLocaleString()} KiB tex${C.reset}`,
);

if (failed > 0) {
  console.log('');
  console.log(`${C.red}${failed} of ${results.length} probes failed${C.reset}`);
  for (const r of results) {
    if (r.result.passed) continue;
    console.log(`  ${C.bold}${r.file}${C.reset}:`);
    for (const f of r.result.failures) {
      console.log(`    ${C.red}${f.stage}/${f.rule}${C.reset}: ${f.detail}`);
    }
  }
  process.exit(1);
}

console.log(`${C.green}all ${results.length} probes passed${C.reset}`);
process.exit(0);

function pct(value, limit) {
  if (value === undefined || value === null) return '—';
  const p = (value / limit) * 100;
  return `${value} (${p.toFixed(0)}%)`;
}

// Padding helper to compensate for ANSI escape codes that don't take width.
function extraPad(s) {
  // Strip ANSI sequences to compute visible length, then pad accordingly.
  const visibleLen = String(s).replace(/\x1b\[[0-9;]*m/g, '').length;
  const totalLen = String(s).length;
  return totalLen - visibleLen;
}
