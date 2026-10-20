import {
  parseClientHello, parseServerHello, parseCertificates, parseServerCertificateVerify,
  verifyCertificateSignature, getSignatureDataTls13,
} from '@reclaimprotocol/tls';
import { records, decryptRecord, sha256 } from './transport.mjs';
import { runProof } from './worker.mjs';
import { verifyServerCertificates } from './certificates.mjs';
import { deflateRawSync, inflateRawSync } from 'node:zlib';

export const SUITE = 'TLS_CHACHA20_POLY1305_SHA256';
const UPDATE = Buffer.from([24, 0, 0, 1, 0]);
function check(ok, message) { if (!ok) throw new Error(message); }
// Repeated record keys compress well, including tiny SSE records. Cap expansion
// before JSON parsing so a miner cannot turn a small claim into an unbounded one.
export function encodeDisclosures(value) { return deflateRawSync(Buffer.from(JSON.stringify(value))).toString('base64'); }
function decodeDisclosures(value) {
  check(typeof value === 'string' && value.length <= 1024 * 1024, 'Invalid compressed disclosures');
  return JSON.parse(inflateRawSync(Buffer.from(value, 'base64'), { maxOutputLength: 24 * 1024 * 1024 }).toString('utf8'));
}
function handshakes(bytes) {
  const messages = [];
  for (let offset = 0; offset < bytes.length;) {
    check(offset + 4 <= bytes.length, 'Truncated handshake');
    const end = offset + 4 + bytes.readUIntBE(offset + 1, 3);
    check(end <= bytes.length, 'Truncated handshake message');
    messages.push({ type: bytes[offset], body: bytes.subarray(offset + 4, end), raw: bytes.subarray(offset, end) });
    offset = end;
  }
  return messages;
}
function visible(record, info) {
  check(info && /^[0-9a-f]{64}$/.test(info.key ?? ''), 'Expected a disclosed ChaCha20 record key');
  return decryptRecord(record, info);
}
function sameEpoch(info, first, number) {
  check(info?.key === first?.key && info?.iv === first?.iv && info?.number === number, 'Unexpected TLS key or sequence');
}
function plain(record, info, type) {
  check(record[0] === type && info?.plain === true && Object.keys(info).length === 1, 'Unexpected plaintext TLS record');
  return record.subarray(5);
}

// All ciphertext comes from the witness's sealed TCP capture, never from the
// miner's JSON. The only hidden records are a token and its authenticated KU.
async function verifyHandshake({ observed, claim, host, rootCAs, proofOptions, allowUnreportedServerRecords = false }) {
  const client = records(observed.client), server = records(observed.server);
  const disclosures = decodeDisclosures(claim.disclosures);
  const dc = disclosures?.client, ds = disclosures?.server;
  check(Array.isArray(dc) && Array.isArray(ds) && dc.length === client.length && (ds.length === server.length || allowUnreportedServerRecords && ds.length < server.length), 'Incomplete disclosures');
  check(client.length >= 2 && server.length >= 3, 'Incomplete TLS handshake');
  const helloBytes = plain(client[0], dc[0], 22);
  const hellos = handshakes(helloBytes);
  check(hellos.length === 1 && hellos[0].type === 1, 'Expected a single ClientHello');
  const hello = parseClientHello(helloBytes);
  check(hello.extensions.SERVER_NAME?.serverName === host, 'Wrong TLS hostname');
  const shMessages = handshakes(plain(server[0], ds[0], 22));
  check(shMessages.length === 1 && shMessages[0].type === 2, 'Expected a single ServerHello');
  const sh = await parseServerHello(shMessages[0].body);
  check(sh.serverTlsVersion === 'TLS1_3' && sh.cipherSuite === SUITE && !sh.supportsPsk, 'Unsupported TLS negotiation');

  let ci = 1, si = 1;
  if (client[ci]?.[0] === 20) check(plain(client[ci], dc[ci++], 20).equals(Buffer.from([1])), 'Invalid CCS');
  if (server[si]?.[0] === 20) check(plain(server[si], ds[si++], 20).equals(Buffer.from([1])), 'Invalid CCS');
  const finish = visible(client[ci], dc[ci]);
  check(dc[ci].number === 0 && finish.type === 22, 'Missing client Finished');
  const clientFinish = handshakes(finish.data);
  check(clientFinish.length === 1 && clientFinish[0].type === 20 && clientFinish[0].body.length === 32, 'Invalid client Finished');
  ci++;

  const transcript = [helloBytes, shMessages[0].raw];
  let handshake = Buffer.alloc(0), step = 0, serverFinished = false, certificates, certificateTimeUnixNs;
  const handshakeKey = ds[si];
  for (let number = 0; si < server.length && !serverFinished; si++, number++) {
    sameEpoch(ds[si], handshakeKey, number);
    const decoded = visible(server[si], ds[si]);
    check(decoded.type === 22, 'Application data before Finished');
    handshake = Buffer.concat([handshake, decoded.data]);
    check(handshake.length <= 128 * 1024, 'Handshake too large');
    while (handshake.length >= 4 && handshake.length >= 4 + handshake.readUIntBE(1, 3)) {
      const length = 4 + handshake.readUIntBE(1, 3);
      const [message] = handshakes(handshake.subarray(0, length));
      const expected = [8, 11, 15, 20][step++];
      check(message.type === expected, 'Unsupported handshake ordering');
      if (message.type === 11) {
        certificates = parseCertificates(message.body, { version: 'TLS1_3' }).certificates;
        check(certificates.length > 0, 'Missing server certificate');
        certificateTimeUnixNs = await verifyServerCertificates(certificates, host, rootCAs, proofOptions);
      } else if (message.type === 15) {
        await verifyCertificateSignature({ ...parseServerCertificateVerify(message.body),
          publicKey: certificates[0].getPublicKey(), signatureData: await getSignatureDataTls13(transcript, SUITE) });
      } else if (message.type === 20) {
        check(message.body.length === 32, 'Invalid server Finished');
        serverFinished = true;
      }
      transcript.push(message.raw);
      handshake = handshake.subarray(length);
      if (serverFinished) check(handshake.length === 0, 'Extra handshake data after Finished');
    }
  }
  check(serverFinished && step === 4, 'Unfinished server authentication');

  return { client, server, dc, ds, ci, si, certificateTimeUnixNs };
}

function initialResponseKey(claim) {
  const key = claim.response_key;
  check(key && /^[0-9a-f]{64}$/.test(key.key ?? '') && /^[0-9a-f]{24}$/.test(key.iv ?? '')
    && key.number === 0, 'Invalid initial response key');
  return { key: key.key, iv: key.iv, number: 0 };
}

export function probeRequest({ host, path }) {
  return Buffer.from(`HEAD ${path} HTTP/1.1\r\nHost: ${host}\r\nConnection: keep-alive\r\n\r\n`);
}

// A fixed, unauthenticated HEAD establishes HTTP keep-alive while the miner
// prepares its proof. No credential or generation request is released here.
export async function authorizeHandshake(options) {
  const { client, server, dc, ci, si } = await verifyHandshake(options);
  check(options.claim.probe === true && ci + 1 === client.length && si === server.length, 'Expected TLS handshake and fixed HEAD probe');
  const probe = visible(client[ci], dc[ci]);
  check(dc[ci].number === 0 && probe.type === 23 && probe.data.equals(probeRequest(options)), 'Invalid HEAD probe');
  return { serverStart: si, responseKey: initialResponseKey(options.claim) };
}

export function probeLength(bytes) {
  const end = bytes.indexOf('\r\n\r\n');
  check(end > 0 && end <= 32768, 'Invalid HEAD response');
  const headers = bytes.subarray(0, end).toString('latin1');
  check(/^HTTP\/1\.1 [2-5][0-9][0-9](?: |\r\n)/.test(headers), 'Invalid HEAD response status');
  check(!/\r\nconnection:[^\r\n]*\bclose\b/i.test(headers), 'Provider closed HEAD probe');
  // HEAD has no body, regardless of its Content-Length header.
  return end + 4;
}

async function verifyRequest(options) {
  const { host, path, authentication = 'subscription', claim, proofOptions } = options;
  const verified = await verifyHandshake(options);
  const { client, server, dc, ds, si } = verified;
  let { ci } = verified;
  if (claim.probe) {
    const probe = visible(client[ci], dc[ci]);
    check(dc[ci].number === 0 && probe.type === 23 && probe.data.equals(probeRequest(options)), 'Invalid HEAD probe');
    ci++;
  }
  const first = dc[ci];
  sameEpoch(first, first, claim.probe ? 1 : 0);
  if (claim.probe) sameEpoch(first, dc[ci - 1], 1);
  const prefix = visible(client[ci++], first);
  check(prefix.type === 23, 'Missing request prefix');
  sameEpoch(dc[ci], first, claim.probe ? 2 : 1);
  const ku = visible(client[ci], dc[ci]); ci++;
  check(ku.type === 22 && ku.data.equals(UPDATE), 'Missing credential key isolation');
  const authIndex = ci;
  check(dc[ci] && dc[ci + 1] && Object.keys(dc[ci]).length === 0 && Object.keys(dc[ci + 1]).length === 0, 'Expected exactly two private records');
  check(client[ci]?.[0] === 23 && client[ci + 1]?.[0] === 23, 'Private TLS record type');
  check(claim.credential?.nonce && claim.credential?.bundle, 'Missing credential proof');
  await runProof('verify', { nonce: claim.credential.nonce, bundle: claim.credential.bundle,
    token_record: client[ci].toString('hex'), update_record: client[ci + 1].toString('hex') }, proofOptions);
  ci += 2;
  const suffixParts = [], bodyKey = dc[ci];
  for (let number = 0; ci < client.length; ci++, number++) {
    sameEpoch(dc[ci], bodyKey, number);
    const decoded = visible(client[ci], dc[ci]);
    check(decoded.type === 23, 'Unexpected client control record');
    suffixParts.push(decoded.data);
  }
  const tokenLength = client[authIndex].length - 22;
  const request = Buffer.concat([prefix.data, Buffer.alloc(tokenLength, '*'), ...suffixParts]);
  const requestBody = parseRequest(request, { host, path, tokenLength, authentication });

  return { requestBody, serverStart: si, server, disclosures: ds, certificateTimeUnixNs: verified.certificateTimeUnixNs };
}

// Verify the complete held generation request and the earlier probe response.
// The provider's HEAD reply confirms the committed response key before spending.
export async function authorizeRequest(options) {
  check(options.claim.probe === true, 'Fixed HEAD probe required');
  const verified = await verifyRequest({ ...options, allowUnreportedServerRecords: true });
  const responseKey = initialResponseKey(options.claim);
  const response = serverApplication(verified.server, verified.serverStart,
    index => ({ ...responseKey, number: index - verified.serverStart }), true);
  check(probeLength(response) === response.length, 'Additional response before generation');
  return { requestBody: verified.requestBody, serverStart: verified.serverStart, responseKey, probe: true,
    certificateTimeUnixNs: verified.certificateTimeUnixNs };
}

function serverApplication(server, si, disclosure, probe = false) {
  const responseParts = [], responseKey = disclosure(si);
  let closed = false, bytes = 0, headLength = 0;
  for (let number = 0; si < server.length; si++, number++) {
    check(!closed, 'Data after TLS close');
    const info = disclosure(si);
    sameEpoch(info, responseKey, number);
    const decoded = visible(server[si], info);
    if (decoded.type === 23) {
      responseParts.push(decoded.data); bytes += decoded.data.length;
      if (probe && !headLength) {
        const head = Buffer.concat(responseParts);
        if (head.indexOf('\r\n\r\n') >= 0) headLength = probeLength(head);
        else check(bytes <= 32768, 'HEAD response too large');
      }
    } else if (decoded.type === 22) {
      check(bytes === 0 || (probe && bytes === headLength), 'Handshake during generation');
      check(handshakes(decoded.data).every(m => m.type === 4), 'Unsupported post-handshake message');
    } else {
      check(decoded.type === 21 && decoded.data.equals(Buffer.from([1, 0])), 'TLS error alert');
      closed = true;
    }
  }
  return Buffer.concat(responseParts);
}

function responsePair(requestBody, server, si, disclosure, probe = false) {
  const response = serverApplication(server, si, disclosure, probe);
  const http = probe ? response.subarray(probeLength(response)) : response;
  const responseStatus = Number(http.subarray(9, 12).toString('ascii'));
  const responseBody = parseResponse(http, responseStatus);
  return { requestBody, responseBody, responseStatus, request_sha256: sha256(requestBody), response_sha256: sha256(responseBody) };
}

// No final disclosure from the miner is needed. The response key is immutable
// from the handshake onward; record sequence numbers come from our capture.
export function finishAuthorizedResponse(serverChunks, authorization) {
  const { requestBody, serverStart, responseKey, probe } = authorization;
  return responsePair(requestBody, records(serverChunks), serverStart,
    index => ({ ...responseKey, number: index - serverStart }), probe);
}

export async function verifyPair(options) {
  const { requestBody, server, serverStart, disclosures } = await verifyRequest(options);
  return responsePair(requestBody, server, serverStart, index => disclosures[index], options.claim.probe);
}

export function requestChunks(body, token, account, { host, path, authentication = 'subscription' }) {
  check(Buffer.isBuffer(body) && body.length <= 8 * 1024 * 1024, 'Request body limit');
  JSON.parse(body.toString('utf8'));
  check(/^[A-Za-z0-9._~+/=-]{64,4096}$/.test(token), 'Unsupported credential format');
  check(['api', 'subscription'].includes(authentication), 'Unknown authentication mode');
  if (authentication === 'subscription') check(/^[a-f0-9-]{1,80}$/.test(account), 'Invalid account identifier');
  return [
    { visible: true, data: Buffer.from(`POST ${path} HTTP/1.1\r\nHost: ${host}\r\nConnection: close\r\nContent-Length: ${body.length}\r\nContent-Type: application/json\r\nAccept: text/event-stream\r\nAuthorization: Bearer `) },
    { visible: false, data: Buffer.from(token) },
    { visible: true, data: Buffer.concat([Buffer.from(authentication === 'api' ? '\r\n\r\n' : `\r\nChatGPT-Account-Id: ${account}\r\n\r\n`), body]) },
  ];
}

function parseRequest(request, { host, path, tokenLength, authentication }) {
  const boundary = request.indexOf('\r\n\r\n');
  check(boundary >= 0 && boundary <= 8192, 'Request headers invalid');
  check(request.subarray(0, boundary).every(byte => byte < 128), 'Non-ASCII request headers');
  const headers = request.subarray(0, boundary).toString('ascii').split('\r\n');
  check(['api', 'subscription'].includes(authentication), 'Unknown authentication mode');
  check(headers.length === (authentication === 'api' ? 7 : 8) && headers[0] === `POST ${path} HTTP/1.1` && headers[1] === `Host: ${host}` && headers[2] === 'Connection: close', 'Request routing/framing mismatch');
  check(/^Content-Length: (0|[1-9][0-9]*)$/.test(headers[3]) && headers[4] === 'Content-Type: application/json' && headers[5] === 'Accept: text/event-stream', 'Unexpected request headers');
  check(headers[6] === `Authorization: Bearer ${'*'.repeat(tokenLength)}` && (authentication === 'api' || /^ChatGPT-Account-Id: [a-f0-9-]{1,80}$/.test(headers[7])), 'Credential coverage mismatch');
  const body = request.subarray(boundary + 4);
  check(body.length <= 8 * 1024 * 1024 && body.length === Number(headers[3].slice(16)), 'Incomplete/additional request body');
  JSON.parse(body.toString('utf8'));
  return body;
}

export function parseResponse(response, expectedStatus = 200) {
  const end = response.indexOf('\r\n\r\n');
  check(end > 0 && end <= 32768, 'Response headers invalid');
  const lines = response.subarray(0, end).toString('latin1').split('\r\n');
  const status = /^HTTP\/1\.1 ([2-5][0-9]{2})(?: |$)/.exec(lines.shift());
  check(status && Number(status[1]) === expectedStatus, 'Unexpected provider HTTP status');
  const headers = new Map();
  for (const line of lines) {
    const match = /^([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*([^\r\n]*)$/.exec(line);
    check(match, 'Invalid response header');
    const name = match[1].toLowerCase();
    check(!headers.has(name) || !['content-length', 'transfer-encoding', 'content-encoding'].includes(name), 'Ambiguous response framing');
    headers.set(name, match[2].trim());
  }
  check(!headers.has('content-encoding') || headers.get('content-encoding') === 'identity', 'Compressed response unsupported');
  const body = response.subarray(end + 4);
  if (headers.has('content-length')) {
    const value = headers.get('content-length');
    check(!headers.has('transfer-encoding') && /^(0|[1-9][0-9]*)$/.test(value) && Number(value) === body.length, 'Incomplete/additional response body');
    return body;
  }
  check(headers.get('transfer-encoding') === 'chunked', 'Response must have explicit framing');
  const parts = [];
  let offset = 0;
  while (offset < body.length) {
    const lineEnd = body.indexOf('\r\n', offset);
    check(lineEnd > offset && lineEnd - offset <= 16, 'Invalid chunk length');
    const size = body.subarray(offset, lineEnd).toString('ascii');
    check(/^[0-9a-fA-F]{1,8}$/.test(size), 'Chunk extensions unsupported');
    const length = Number.parseInt(size, 16);
    offset = lineEnd + 2;
    check(offset + length + 2 <= body.length && body.subarray(offset + length, offset + length + 2).equals(Buffer.from('\r\n')), 'Incomplete chunk');
    if (length === 0) { check(offset + 2 === body.length, 'Trailers/additional response unsupported'); return Buffer.concat(parts); }
    parts.push(body.subarray(offset, offset + length));
    offset += length + 2;
  }
  throw new Error('Missing terminal response chunk');
}
