import assert from 'node:assert/strict';
import test from 'node:test';
import {build} from 'esbuild';

const output = await build({entryPoints: ['src/timing.ts'], bundle: true, write: false, format: 'esm'});
const {measure} = await import('data:text/javascript;base64,' + Buffer.from(output.outputFiles[0].text).toString('base64'));

test('a failing timing sink preserves the result and the original rejection', async () => {
  const original = console.log;
  console.log = () => {throw new Error('Diagnostic sink failed');};
  try {
    assert.equal(await measure('fixture', 'dispatch', async () => 42), 42);
    const failure = new Error('Original failure');
    await assert.rejects(measure('fixture', 'dispatch', async () => {throw failure;}), error => error === failure);
  } finally {console.log = original;}
});
