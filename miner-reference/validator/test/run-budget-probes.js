#!/usr/bin/env node
/**
 * Budget probe runner.
 *
 * Each fixture in `budget-probes/` is a hand-written `generate.js` of a
 * different complexity class. The runner serves three purposes:
 *
 *   1. **CI gate.** All probes must validate cleanly. If a spec or validator
 *      change starts rejecting one of them, this script fails the build, so
 *      the impact is visible before merge.
 *
 *   2. **Headroom snapshot.** The summary table prints how much of each cap
 *      every probe uses, so you can see where realistic outputs sit relative
 *      to the limits and tune the caps with evidence.
 *
 *   3. **Metric drift detection.** Each fixture may declare expected metric
 *      values via header annotations. If the validator's reported metrics
 *      drift outside the declared range, the runner fails the probe with a
 *      "metric drift" message even if validation passed. This catches
 *      semantic regressions in the post-validation walk that pass/fail
 *      assertions alone would miss (the bare-Mesh wrap bug being a recent
 *      example — depth quietly went from 1 to 0 without any pass/fail
 *      change).
 *
 * Annotation format (one per metric, anywhere in the file as a line comment):
 *
 *     // @vertices 1497          (exact match)
 *     // @drawCalls 6
 *     // @maxDepth 1
 *     // @instances 0
 *     // @textureBytes 0
 *     // @vertices 1490-1500     (inclusive range)
 *
 * Supported metrics: vertices, drawCalls, maxDepth, instances, textureBytes.
 * Annotations are optional per fixture; missing annotations are not checked.
 *
 * Run from the validator package root:
 *   node test/run-budget-probes.js
 *
 * Exit codes:
 *   0  — every probe validated cleanly and (where annotated) matched its
 *        declared metric ranges
 *   1  — one or more probes failed validation or drifted from annotations
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { validate } from '../src/index.js';
import { LIMITS } from '../src/postValidation.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Scan three directories so the runner serves both purposes:
//   - validator/test/budget-probes/  — synthetic complexity-tier fixtures
//   - examples/                      — discoverable miner-facing reference
//                                      examples covering each allowed
//                                      pattern (custom BufferGeometry,
//                                      Points, LineSegments, material
//                                      variants, etc.)
//   - examples/generated/            — real prompt-to-output pairs from a
//                                      VLM + code-LLM pipeline
//
// All three contribute to CI coverage and metric drift detection.
const SOURCE_DIRS = [
  { label: 'budget-probes',     dir: path.join(__dirname, 'budget-probes') },
  { label: 'examples',          dir: path.join(__dirname, '..', '..', 'examples') },
  { label: 'examples/generated', dir: path.join(__dirname, '..', '..', 'examples', 'generated') },
];

const KNOWN_METRICS = new Set([
  'vertices',
  'drawCalls',
  'maxDepth',
  'instances',
  'textureBytes',
]);

const probeFiles = [];
for (const { label, dir } of SOURCE_DIRS) {
  let entries;
  try {
    entries = await fs.readdir(dir);
  } catch (err) {
    if (err.code === 'ENOENT') continue;
    throw err;
  }
  for (const entry of entries.filter((f) => f.endsWith('.js')).sort()) {
    probeFiles.push({
      label,
      file: entry,
      fullPath: path.join(dir, entry),
      displayName: `${label}/${entry}`,
    });
  }
}

if (probeFiles.length === 0) {
  console.error(`No probe fixtures found in any of: ${SOURCE_DIRS.map(s => s.dir).join(', ')}`);
  process.exit(1);
}

const C = process.stdout.isTTY
  ? { red: '\x1b[31m', green: '\x1b[32m', yellow: '\x1b[33m', dim: '\x1b[2m', bold: '\x1b[1m', reset: '\x1b[0m' }
  : { red: '', green: '', yellow: '', dim: '', bold: '', reset: '' };

const results = [];
let failed = 0;

for (const probe of probeFiles) {
  const source = await fs.readFile(probe.fullPath, 'utf8');
  const annotations = parseAnnotations(source);
  const result = await validate(source);

  let mismatches = [];
  let passed = result.passed;
  if (passed && Object.keys(annotations).length > 0 && result.metrics) {
    mismatches = checkAnnotations(result.metrics, annotations);
    if (mismatches.length > 0) passed = false;
  }

  results.push({
    file: probe.displayName,
    result,
    annotations,
    mismatches,
    passed,
  });
  if (!passed) failed++;
}

// Pretty summary table
console.log('');
console.log(`${C.bold}Budget probe results${C.reset}`);
console.log('');

const cols = [
  ['fixture',   42, (r) => r.file],
  ['verts',      9, (r) => pct(r.result.metrics?.vertices, LIMITS.vertices)],
  ['draws',      8, (r) => pct(r.result.metrics?.drawCalls, LIMITS.drawCalls)],
  ['depth',      8, (r) => pct(r.result.metrics?.maxDepth, LIMITS.depth)],
  ['inst',       9, (r) => pct(r.result.metrics?.instances, LIMITS.instances)],
  ['tex',        9, (r) => pct(r.result.metrics?.textureBytes, LIMITS.textureBytes)],
  ['ms',         6, (r) => `${r.result.executionMs}`],
  ['status',     8, (r) => statusLabel(r)],
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

const annotated = results.filter(r => Object.keys(r.annotations).length > 0).length;
console.log(
  `${C.dim}metric annotations: ${annotated} of ${results.length} probes have @-annotations${C.reset}`,
);

if (failed > 0) {
  console.log('');
  console.log(`${C.red}${failed} of ${results.length} probes failed${C.reset}`);
  for (const r of results) {
    if (r.passed) continue;
    console.log(`  ${C.bold}${r.file}${C.reset}:`);
    for (const f of r.result.failures) {
      console.log(`    ${C.red}${f.stage}/${f.rule}${C.reset}: ${f.detail}`);
    }
    for (const m of r.mismatches) {
      console.log(
        `    ${C.red}metric drift${C.reset}: ${m.metric} = ${m.actual}, expected ${m.expected}`,
      );
    }
  }
  process.exit(1);
}

console.log(`${C.green}all ${results.length} probes passed${C.reset}`);
process.exit(0);

function statusLabel(r) {
  if (!r.passed) return `${C.red}FAIL${C.reset}`;
  return `${C.green}PASS${C.reset}`;
}

function pct(value, limit) {
  if (value === undefined || value === null) return '—';
  const p = (value / limit) * 100;
  return `${value} (${p.toFixed(0)}%)`;
}

/**
 * Parse `// @<metric> <value>` lines from the fixture source.
 * Returns a map of metric name → { min, max } range objects.
 *
 * Value formats:
 *   `123`     — exact integer (min === max === 123)
 *   `100-200` — inclusive integer range
 */
function parseAnnotations(source) {
  const annotations = {};
  const re = /^\/\/\s*@(\w+)\s+(\S+)/gm;
  let match;
  while ((match = re.exec(source)) !== null) {
    const [, key, value] = match;
    if (!KNOWN_METRICS.has(key)) continue;
    const range = parseRange(value);
    if (range !== null) annotations[key] = range;
  }
  return annotations;
}

function parseRange(value) {
  const m = value.match(/^(\d+)(?:-(\d+))?$/);
  if (!m) return null;
  const min = parseInt(m[1], 10);
  const max = m[2] !== undefined ? parseInt(m[2], 10) : min;
  return { min, max };
}

/**
 * Compare actual metrics to declared annotations.
 * Returns an array of { metric, actual, expected } mismatches.
 */
function checkAnnotations(metrics, annotations) {
  const mismatches = [];
  for (const [key, range] of Object.entries(annotations)) {
    const actual = metrics[key];
    if (actual === undefined) continue;
    if (actual < range.min || actual > range.max) {
      mismatches.push({
        metric: key,
        actual,
        expected: range.min === range.max ? `${range.min}` : `${range.min}–${range.max}`,
      });
    }
  }
  return mismatches;
}

// Padding helper to compensate for ANSI escape codes that don't take width.
function extraPad(s) {
  const visibleLen = String(s).replace(/\x1b\[[0-9;]*m/g, '').length;
  const totalLen = String(s).length;
  return totalLen - visibleLen;
}
