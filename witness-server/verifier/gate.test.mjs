import test from 'node:test';
import assert from 'node:assert/strict';
import { responseUsage, providerRejected } from './gate.mjs';
import { parseResponse } from './verify.mjs';

const usage = { input_tokens: 20, output_tokens: 40, total_tokens: 60 };
const event = (status, tokens = usage) => ({ type: `response.${status}`, response: { status, usage: tokens } });
const sse = (...events) => Buffer.from(events.map(value => `data: ${JSON.stringify(value)}\n\n`).join(''));

test('terminal usage is accounted even when generation failed or was incomplete', () => {
  for (const status of ['completed', 'failed', 'incomplete']) {
    assert.deepEqual(responseUsage(sse(event(status))), usage);
    for (const invalid of [null, {}, { ...usage, output_tokens: -1 }, { ...usage, total_tokens: 0 }]) {
      assert.equal(responseUsage(sse(event(status, invalid))), null);
    }
  }
  assert.equal(responseUsage(sse(event('completed'), event('failed'))), null);
  assert.equal(responseUsage(sse({ type: 'response.failed', response: { status: 'completed', usage } })), null);
  assert.equal(responseUsage(sse({ type: 'response.in_progress', response: { status: 'in_progress', usage } })), null);
});

test('only authenticated request/resource/auth/access/rate rejections lift the unknown-usage hold', () => {
  for (const status of [400, 401, 403, 404, 429]) assert.equal(providerRejected(status), true);
  for (const status of [undefined, null, 200, 402, 405, 408, 409, 422, 500, 502, 503, 504]) assert.equal(providerRejected(status), false);
});

test('non-200 evidence still requires a complete, unambiguous HTTP body', () => {
  for (const status of [400, 401, 403, 404, 429, 500]) {
    const response = Buffer.from(`HTTP/1.1 ${status} Error\r\nContent-Length: 2\r\n\r\n{}`);
    assert.equal(parseResponse(response, status).toString(), '{}');
    assert.throws(() => parseResponse(response)); // Generation clients still require success.
    assert.throws(() => parseResponse(response.subarray(0, -1), status));
    assert.throws(() => parseResponse(Buffer.concat([response, Buffer.from('extra')]), status));
    const ambiguous = Buffer.from(`HTTP/1.1 ${status} Error\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}`);
    assert.throws(() => parseResponse(ambiguous, status));
  }
  assert.throws(() => parseResponse(Buffer.from('HTTP/1.1 999 Error\r\nContent-Length: 0\r\n\r\n'), 999));
});
