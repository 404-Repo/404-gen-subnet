#!/usr/bin/env node
/**
 * 404-subnet miner conformance CLI.
 *
 * Usage:
 *   node tools/validate.js <path/to/generate.js>
 *   node tools/validate.js --json <path/to/generate.js>
 *
 * Exit codes:
 *   0 — passed all checks
 *   1 — one or more failures
 *   2 — usage error
 *
 * The output mirrors the production runtime's failure shape:
 *   { stage, rule, detail }
 *
 * See ../validator/README.md for the rules this tool enforces and the ones
 * it can't (heap cap, render run, container isolation — those live in the
 * production validator only).
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const args = process.argv.slice(2);
let jsonOutput = false;
const positional = [];
for (const arg of args) {
  if (arg === '--json') jsonOutput = true;
  else if (arg === '-h' || arg === '--help') usage(0);
  else positional.push(arg);
}

if (positional.length !== 1) usage(2);

const filePath = path.resolve(positional[0]);

let source;
try {
  source = await fs.readFile(filePath, 'utf8');
} catch (err) {
  process.stderr.write(`error: cannot read ${filePath}: ${err.message}\n`);
  process.exit(2);
}

let validate;
try {
  ({ validate } = await import(
    path.join(__dirname, '..', 'validator', 'src', 'index.js')
  ));
} catch (err) {
  process.stderr.write(
    `error: failed to load validator package. Did you run \`cd validator && npm install\`?\n` +
      `details: ${err.message}\n`,
  );
  process.exit(2);
}

const result = await validate(source);

if (jsonOutput) {
  console.log(
    JSON.stringify({ file: filePath, ...result }, null, 2),
  );
  process.exit(result.passed ? 0 : 1);
}

// Pretty output
const C = colorize();
const status = result.passed
  ? `${C.green}PASSED${C.reset}`
  : `${C.red}FAILED${C.reset}`;

console.log(`${C.bold}${path.relative(process.cwd(), filePath)}${C.reset}: ${status}`);
console.log(`stages run: ${result.stagesRun.join(' → ')}`);

if (result.metrics) {
  const m = result.metrics;
  console.log('');
  console.log(`${C.bold}metrics${C.reset}`);
  console.log(`  vertices:     ${pad(m.vertices)} / 250,000`);
  console.log(`  draw calls:   ${pad(m.drawCalls)} / 200`);
  console.log(`  scene depth:  ${pad(m.maxDepth)} / 32`);
  console.log(`  instances:    ${pad(m.instances)} / 50,000`);
  console.log(`  texture data: ${pad(m.textureBytes)} / 1,048,576 bytes`);
  if (m.bbox) {
    const { min, max } = m.bbox;
    const fits =
      min.x >= -0.5 && max.x <= 0.5 &&
      min.y >= -0.5 && max.y <= 0.5 &&
      min.z >= -0.5 && max.z <= 0.5;
    const mark = fits ? `${C.green}✓${C.reset}` : `${C.red}✗${C.reset}`;
    console.log(
      `  bbox:         [${f(min.x)}, ${f(min.y)}, ${f(min.z)}] → ` +
        `[${f(max.x)}, ${f(max.y)}, ${f(max.z)}]  ${mark}`,
    );
  }
  console.log(`  generate():   ${result.executionMs} ms / 5,000 ms`);
}

if (result.failures.length > 0) {
  console.log('');
  console.log(`${C.bold}failures${C.reset}`);
  for (const f of result.failures) {
    console.log(
      `  ${C.red}${f.stage}${C.reset}/${C.bold}${f.rule}${C.reset}: ${f.detail}`,
    );
  }
}

process.exit(result.passed ? 0 : 1);

function usage(code) {
  process.stderr.write(
    `Usage: node tools/validate.js [--json] <path/to/generate.js>\n\n` +
      `Validate a miner generate.js against the 404-subnet specifications.\n` +
      `--json   emit a single JSON object instead of pretty output\n` +
      `--help   show this message\n`,
  );
  process.exit(code);
}

function pad(n) {
  return String(n).padStart(7, ' ');
}

function f(n) {
  return n.toFixed(3);
}

function colorize() {
  const isTTY = process.stdout.isTTY;
  if (!isTTY) {
    return { red: '', green: '', bold: '', reset: '' };
  }
  return {
    red: '\x1b[31m',
    green: '\x1b[32m',
    bold: '\x1b[1m',
    reset: '\x1b[0m',
  };
}
