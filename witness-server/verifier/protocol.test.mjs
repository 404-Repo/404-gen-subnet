import test from 'node:test';
import assert from 'node:assert/strict';
import https from 'node:https';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadX509FromPem } from '@reclaimprotocol/tls';
import { exchange, listen, relay, sha256 } from './transport.mjs';
import { verifyPair, requestChunks, SUITE, parseRequest, parseResponse, encodeDisclosures } from './verify.mjs';
import { deflateRawSync } from 'node:zlib';
import { makeCredentialProof } from './worker.mjs';

export async function fixture({ requestBytes = 131072, responseBytes = 1048576, attack = false } = {}) {
  const directory = mkdtempSync(join(tmpdir(), '404-pair-'));
  let provider, witness;
  const sockets = new Set();
  try {
    execFileSync('openssl', ['req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-sha256', '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost', '-keyout', join(directory, 'key'), '-out', join(directory, 'cert')], { stdio: 'ignore', timeout: 5000 });
    const cert = readFileSync(join(directory, 'cert'), 'utf8'), rootCAs = [loadX509FromPem(cert)];
    const request = Buffer.from(JSON.stringify({ model: 'synthetic', input: 'x'.repeat(requestBytes - 32) }));
    const response = Buffer.from(JSON.stringify({ model: 'synthetic', output: 'x'.repeat(responseBytes - 33) }));
    let actual;
    provider = https.createServer({ key: readFileSync(join(directory, 'key')), cert, minVersion: 'TLSv1.3', maxVersion: 'TLSv1.3', ciphers: 'TLS_CHACHA20_POLY1305_SHA256' }, (req, res) => {
      const chunks = [];
      req.on('data', b => chunks.push(b));
      req.on('end', () => { actual = Buffer.concat(chunks); req.socket.pause(); setTimeout(() => { res.writeHead(200, { 'content-type': 'application/json', 'content-length': response.length, 'connection': 'close' }); res.end(response); }, 25); });
    });
    provider.maxRequestsPerSocket = 1;
    provider.on('connection', s => { sockets.add(s); s.on('close', () => sockets.delete(s)); });
    witness = await relay(await listen(provider));
    const chunks = requestChunks(request, 'DUMMY_' + 't'.repeat(1516), 'abc123', { host: 'localhost', path: '/fixture' });
    if (attack) chunks[1].data = Buffer.concat([chunks[1].data, Buffer.from('\r\n\r\n'), request]);
    const started = performance.now();
    const miner = await exchange({ port: witness.port, rootCAs, cipherSuite: SUITE, capturePrivate: true, chunks });
    const transferMs = performance.now() - started;
    const proofStart = performance.now();
    const credential = await makeCredentialProof(miner.privateRecords);
    const provingMs = performance.now() - proofStart;
    const claim = { disclosures: encodeDisclosures(miner.disclosures), credential };
    return {
      observed: witness.observed, claim, host: 'localhost', path: '/fixture', rootCAs,
      expected: { request, response, actual }, metrics: { transfer_ms: transferMs, proving_ms: provingMs, proof_bytes: Buffer.byteLength(JSON.stringify(credential)), wire_bytes: witness.bytes.client + witness.bytes.server, disclosure_bytes: Buffer.byteLength(JSON.stringify(claim)) },
    };
  } finally {
    witness?.close();
    for (const s of sockets) s.destroy();
    provider?.close(); rmSync(directory, { recursive: true, force: true });
  }
}

test('full private-credential proof authenticates observed request and response', { timeout: 120000 }, async t => {
  const data = await fixture();
  const start = performance.now();
  const result = await verifyPair(data);
  t.diagnostic(JSON.stringify({ ...data.metrics, verification_ms: performance.now() - start }));
  assert.ok(result.requestBody.equals(data.expected.actual));
  assert.ok(result.responseBody.equals(data.expected.response));
  assert.equal(result.request_sha256, sha256(data.expected.request));
  await t.test('substituted certificate trust or destination rejected', async () => {
    await assert.rejects(verifyPair({ ...data, rootCAs: [] }));
    await assert.rejects(verifyPair({ ...data, host: 'wrong.example' }));
  });
  await t.test('changed observed ciphertext rejected', async () => {
    const observed = structuredClone(data.observed);
    observed.client = observed.client.map(b => Buffer.from(b)); observed.server = observed.server.map(b => Buffer.from(b));
    observed.server.at(-1)[10] ^= 1;
    await assert.rejects(verifyPair({ ...data, observed }));
  });
  await t.test('missing credential proof rejected', async () => {
    const claim = structuredClone(data.claim); claim.credential.bundle.proofs.pop();
    await assert.rejects(verifyPair({ ...data, claim }));
  });
  await t.test('oversized disclosure expansion rejected', async () => {
    const claim = { ...data.claim, disclosures: deflateRawSync(Buffer.alloc(25 * 1024 * 1024)).toString('base64') };
    await assert.rejects(verifyPair({ ...data, claim }), /larger than|Cannot create a Buffer/);
  });
});

test('hidden-request attack cannot produce a credential proof', { timeout: 120000 }, async () => {
  await assert.rejects(fixture({ requestBytes: 1024, responseBytes: 2048, attack: true }), /credential constraints failed/);
});

test('HTTP parser requires complete unambiguous framing', () => {
  const make = (headers, body) => Buffer.from(`HTTP/1.1 200 OK\r\n${headers}\r\n\r\n${body}`);
  assert.equal(parseResponse(make('Transfer-Encoding: chunked', '3\r\nabc\r\n0\r\n\r\n')).toString(), 'abc');
  for (const bad of [make('Content-Length: 3', 'ab'), make('Content-Length: 2', 'abc'), make('Content-Length: 3\r\nTransfer-Encoding: chunked', 'abc'), make('Transfer-Encoding: chunked', '3\r\nabc\r\n')]) assert.throws(() => parseResponse(bad));
});

test('request allowlist permits only a bounded optional subscription session-id', () => {
  const body = Buffer.from('{"model":"synthetic","prompt_cache_key":"body-key"}');
  const target = { host: 'chatgpt.com', path: '/backend-api/codex/responses', tokenLength: 4096, authentication: 'subscription' };
  const make = (extra = '', authentication = 'subscription') => Buffer.concat([
    Buffer.from(`POST ${target.path} HTTP/1.1\r\nHost: ${target.host}\r\nConnection: close\r\nContent-Length: ${body.length}\r\nContent-Type: application/json\r\nAccept: text/event-stream\r\nAuthorization: Bearer ${'*'.repeat(target.tokenLength)}\r\n${authentication === 'subscription' ? `ChatGPT-Account-Id: ${'a'.repeat(80)}\r\n` : ''}${extra}\r\n`), body,
  ]);
  for (const extra of ['', 'session-id: fixture-session\r\n', `session-id: ${'s'.repeat(1024)}\r\n`]) {
    assert.ok(parseRequest(make(extra), target).equals(body));
  }
  assert.ok(parseRequest(make('', 'api'), { ...target, authentication: 'api' }).equals(body));
  for (const value of ['', 'has space', 'a\t', 'a\x00', 'a\x7f', 'é', 'a\n', 'a\r', 'a'.repeat(1025), 'a\r\nX: injected', 'a\r\nsession-id: duplicate']) {
    assert.throws(() => parseRequest(make(`session-id: ${value}\r\n`), target));
  }
  for (const extra of ['session_id: wrong-spelling\r\n', 'Cookie: injected\r\n', 'session-id: a\r\nCookie: injected\r\n']) {
    assert.throws(() => parseRequest(make(extra), target));
  }
  assert.throws(() => parseRequest(make('session-id: a\r\n', 'api'), { ...target, authentication: 'api' }));
});
