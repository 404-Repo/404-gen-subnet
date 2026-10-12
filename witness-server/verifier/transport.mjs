// TLS 1.3 record transport and local verification fixtures.
import net from 'node:net';
import { once } from 'node:events';
import { createDecipheriv, createHash } from 'node:crypto';
import { makeTLSClient, setCryptoImplementation, crypto as tlsCrypto } from '@reclaimprotocol/tls';
import { webcryptoCrypto } from '@reclaimprotocol/tls/webcrypto';
import { verifyServerCertificates } from './certificates.mjs';

setCryptoImplementation(webcryptoCrypto);
export const MAX_BYTES = 16 * 1024 * 1024;
const MAX_RECORDS = 65536;
const logger = Object.fromEntries(['trace', 'debug', 'info', 'warn', 'error'].map(k => [k, () => {}]));
logger.child = () => logger;
export const sha256 = value => createHash('sha256').update(value).digest('hex');

export async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}

// An actual TCP relay, with backpressure and fixed loopback destination.
// No destination, credentials or TLS secrets arrive from the miner.
export async function relay(port) {
  const observed = { client: [], server: [] };
  const bytes = { client: 0, server: 0 };
  const sockets = new Set();
  const server = net.createServer(client => {
    const upstream = net.connect(port, '127.0.0.1');
    for (const socket of [client, upstream]) {
      sockets.add(socket);
      socket.setTimeout(15000, () => socket.destroy());
      socket.on('close', () => sockets.delete(socket));
      socket.on('error', () => { client.destroy(); upstream.destroy(); });
    }
    client.on('close', () => upstream.destroy());
    upstream.on('close', () => client.destroy());
    for (const [side, socket] of [['client', client], ['server', upstream]]) {
      socket.on('data', chunk => {
        bytes[side] += chunk.length;
        if (bytes[side] > MAX_BYTES) { client.destroy(); upstream.destroy(); return; }
        observed[side].push(chunk);
      });
    }
    client.pipe(upstream).pipe(client);
  });
  return {
    port: await listen(server), observed, bytes,
    close() { for (const socket of sockets) socket.destroy(); server.close(); },
  };
}

// The miner exports RECORD encryption keys, never TLS traffic/master secrets.
// KeyUpdate isolates opaque chunks from the keys that are later disclosed.
export async function exchange({ port, host = 'localhost', rootCAs, chunks, update = true,
  cipherSuite = 'TLS_AES_128_GCM_SHA256', capturePrivate = false,
  connectHost, onCiphertext, onReceivedCiphertext, socket: suppliedSocket,
  timeoutMs = 15000, proofOptions, handshake: discloseHandshake, authorize,
}) {
  const socket = suppliedSocket ?? new net.Socket();
  const disclosures = { client: [], server: [] };
  const received = [];
  let receivedBytes = 0, reveal = true, reading = Promise.resolve();
  let metadata, certificates;
  let settle, fail;
  const done = new Promise((resolve, reject) => { settle = resolve; fail = reject; });
  // Attach a handler immediately: failure can precede the handshake await.
  done.catch(() => {});
  const timer = setTimeout(() => fail(new Error('TLS exchange deadline exceeded')), timeoutMs);
  const pendingExports = [];
  const privateRecords = [];
  let probeReady, probeComplete = false;
  const probeReceived = new Promise(resolve => { probeReady = resolve; });
  function disclose(side, ctx, visible, wire) {
    if (disclosures[side].length >= MAX_RECORDS) throw new Error('Record limit exceeded');
    const info = {};
    disclosures[side].push(info);
    if (ctx.type === 'plaintext') { info.plain = true; return; }
    if (!visible) {
      if (capturePrivate) pendingExports.push(tlsCrypto.exportKey(ctx.encKey).then(key => {
        privateRecords.push({ index: disclosures[side].indexOf(info),
          key: Buffer.from(key).toString('hex'), nonce: Buffer.from(ctx.fixedIv).toString('hex'),
          number: ctx.recordNumber, record: wire.toString('hex') });
      }));
      return;
    }
    // Copy only the disclosure. Never persist ctx, which contains plaintext.
    pendingExports.push(tlsCrypto.exportKey(ctx.encKey).then(key => {
      Object.assign(info, {
        key: Buffer.from(key).toString('hex'),
        iv: Buffer.from(ctx.fixedIv).toString('hex'), number: ctx.recordNumber,
      });
    }));
  }
  let handshook;
  const handshake = new Promise(resolve => { handshook = resolve; });
  const tls = makeTLSClient({
    // Replace the dependency's incomplete chain validator. CertificateVerify
    // and Finished are still checked by TLS; our chain check gates application writes.
    host, logger, verifyServerCertificate: false,
    supportedProtocolVersions: ['TLS1_3'], cipherSuites: [cipherSuite],
    applicationLayerProtocols: ['http/1.1'],
    write({ header, content }, ctx) {
      const wire = Buffer.concat([header, content]);
      disclose('client', ctx, reveal, wire);
      onCiphertext?.(wire);
      return new Promise((resolve, reject) => socket.write(
        wire, error => error ? reject(error) : resolve(),
      ));
    },
    onRead(_packet, ctx) { disclose('server', ctx, true); },
    onRecvCertificates(result) { certificates = result.certificates; },
    onHandshake() { metadata = tls.getMetadata(); handshook(); },
    onApplicationData(data) {
      receivedBytes += data.length;
      if (receivedBytes > MAX_BYTES) { fail(new Error('Response limit exceeded')); return; }
      received.push(Buffer.from(data));
      if (discloseHandshake && !probeComplete) {
        const head = Buffer.concat(received), end = head.indexOf('\r\n\r\n');
        if (head.length > 32768 || (end >= 0 && end + 4 !== head.length)) {
          fail(new Error('Invalid HEAD probe response')); return;
        }
        if (end >= 0) { probeComplete = true; probeReady(); }
      }
    },
    onTlsEnd(error) { if (error) fail(error); },
  });
  socket.on('data', bytes => {
    onReceivedCiphertext?.(bytes);
    reading = reading.then(() => tls.handleReceivedBytes(bytes));
    reading.catch(fail);
  });
  socket.once('error', fail);
  socket.once('end', () => reading.then(settle, fail));
  socket.once('close', () => reading.then(settle, fail));
  try {
    if (!suppliedSocket) {
      socket.connect(port, connectHost ?? (host === 'localhost' ? '127.0.0.1' : host));
      await Promise.race([once(socket, 'connect'), done.then(() => { throw new Error('Early close'); })]);
    }
    await tls.startHandshake();
    await Promise.race([handshake, done.then(() => { throw new Error('Early handshake close'); })]);
    await Promise.race([verifyServerCertificates(certificates, host, rootCAs, proofOptions),
      done.then(() => { throw new Error('Early certificate-validation close'); })]);
    if (discloseHandshake) {
      const path = chunks[0].data.toString('ascii').split(' ', 3)[1];
      await tls.write(Buffer.from(`HEAD ${path} HTTP/1.1\r\nHost: ${host}\r\nConnection: keep-alive\r\n\r\n`));
      await Promise.all(pendingExports);
      const keys = tls.getKeys();
      await discloseHandshake({ disclosures, response_key: {
        key: Buffer.from(await tlsCrypto.exportKey(keys.serverEncKey)).toString('hex'),
        iv: Buffer.from(keys.serverIv).toString('hex'), number: keys.recordRecvCount,
      } });
      await Promise.race([probeReceived, done.then(() => { throw new Error('Provider closed HEAD probe'); })]);
      received.length = 0; receivedBytes = 0;
    }
    for (const chunk of chunks) {
      if (chunk.visible !== reveal && update) await tls.updateTrafficKeys();
      reveal = chunk.visible;
      await tls.write(chunk.data);
    }
    if (authorize) {
      await Promise.all(pendingExports);
      const keys = tls.getKeys();
      await authorize({ disclosures, privateRecords, response_key: {
        key: Buffer.from(await tlsCrypto.exportKey(keys.serverEncKey)).toString('hex'),
        iv: Buffer.from(keys.serverIv).toString('hex'), number: discloseHandshake ? 0 : keys.recordRecvCount,
      } });
    }
    await done;
    await reading;
    await Promise.all(pendingExports);
    return { disclosures, response: Buffer.concat(received), metadata, privateRecords };
  } finally {
    clearTimeout(timer);
    socket.destroy();
  }
}

export function records(chunks) {
  const wire = Buffer.concat(chunks);
  if (wire.length > MAX_BYTES) throw new Error('Transcript limit exceeded');
  const result = [];
  for (let offset = 0; offset < wire.length;) {
    if (offset + 5 > wire.length) throw new Error('Truncated TLS header');
    const length = wire.readUInt16BE(offset + 3);
    if (length > 16640 || offset + 5 + length > wire.length) throw new Error('Invalid TLS record');
    result.push(wire.subarray(offset, offset + 5 + length));
    if (result.length > MAX_RECORDS) throw new Error('Record limit exceeded');
    offset += 5 + length;
  }
  return result;
}

// Independent Node/OpenSSL decryption of WITNESS-observed ciphertext.
// Does not authenticate the handshake or prove what is inside opaque records.
export function decryptRecord(record, info) {
  if (!/^(?:[0-9a-f]{32}|[0-9a-f]{64})$/.test(info.key ?? '') || !/^[0-9a-f]{24}$/.test(info.iv ?? '')) {
    throw new Error('Invalid record key or IV');
  }
  if (!Number.isSafeInteger(info.number) || info.number < 0) throw new Error('Invalid sequence');
  const nonce = Buffer.from(info.iv, 'hex');
  const sequence = Buffer.alloc(12);
  sequence.writeBigUInt64BE(BigInt(info.number), 4);
  for (let i = 0; i < nonce.length; i++) nonce[i] ^= sequence[i];
  const cipher = createDecipheriv(info.key.length === 64 ? 'chacha20-poly1305' : 'aes-128-gcm', Buffer.from(info.key, 'hex'), nonce, { authTagLength: 16 });
  cipher.setAAD(record.subarray(0, 5));
  cipher.setAuthTag(record.subarray(-16));
  const plaintext = Buffer.concat([cipher.update(record.subarray(5, -16)), cipher.final()]);
  let end = plaintext.length - 1;
  while (end >= 0 && plaintext[end] === 0) end--;
  if (end < 0) throw new Error('Missing TLS inner type');
  return { type: plaintext[end], data: plaintext.subarray(0, end) };
}

export function disclosedApplication(observed, disclosures) {
  const output = {}, opaque = [];
  for (const side of ['client', 'server']) {
    const wire = records(observed[side]);
    if (wire.length !== disclosures[side].length) throw new Error('Disclosure count mismatch');
    const parts = [];
    for (let i = 0; i < wire.length; i++) {
      const info = disclosures[side][i], record = wire[i];
      if (record[0] !== 23) {
        if (!info.plain) throw new Error('Unexpected plaintext record');
      } else if (!info.key) {
        if (side !== 'client') throw new Error('Opaque response record');
        // This is the UNSAFE assumption under test, not authenticated plaintext.
        const length = record.length - 5 - 16 - 1;
        if (length < 0 || length > 4096) throw new Error('Opaque record exceeds credential cap');
        parts.push(Buffer.alloc(length, '*'));
        opaque.push(record);
      } else {
        const decoded = decryptRecord(record, info);
        if (decoded.type === 23) parts.push(decoded.data);
        else if (![21, 22].includes(decoded.type)) throw new Error('Unsupported inner type');
      }
    }
    output[side] = Buffer.concat(parts);
  }
  return { ...output, opaque };
}

// Negative-test helper: accepts unauthenticated disclosures to demonstrate
// why record decryption alone is insufficient. Use verify.mjs for verification.
export function candidateBody(request) {
  const end = request.indexOf('\r\n\r\n');
  if (end < 0 || end > 8192) throw new Error('Invalid headers');
  const header = request.subarray(0, end).toString('ascii');
  const match = /^POST \/fixture HTTP\/1\.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: ([0-9]+)\r\nContent-Type: application\/json\r\nAuthorization: Bearer (\*+)$/.exec(header);
  if (!match || match[2].length > 4096) throw new Error('Unexpected request framing');
  const body = request.subarray(end + 4);
  if (body.length !== Number(match[1])) throw new Error('Body length mismatch');
  JSON.parse(body.toString());
  return body;
}

export function requireAttestableRequest(disclosed) {
  if (disclosed.opaque.length) throw new Error('Opaque request bytes cannot establish HTTP request boundaries');
  throw new Error('Experimental transport has no complete attestation verifier');
}
