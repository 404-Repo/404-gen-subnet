import test from 'node:test';
import assert from 'node:assert/strict';
import https from 'node:https';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadX509FromPem } from '@reclaimprotocol/tls';
import { exchange, listen, relay } from './transport.mjs';
import { verifyServerCertificates } from './certificates.mjs';
import { verifyPair, requestChunks, encodeDisclosures, SUITE } from './verify.mjs';

test('non-CA issuer cannot receive a credential or obtain a witness receipt', { timeout: 30000 }, async () => {
  const dir = mkdtempSync(join(tmpdir(), '404-invalid-ca-'));
  let provider, witness, requests = 0, applicationBytes = 0;
  const sockets = new Set();
  try {
    const openssl = args => execFileSync('openssl', args, { cwd: dir, stdio: 'ignore', timeout: 5000 });
    const csr = (name, cn) => openssl(['req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-subj', `/CN=${cn}`, '-keyout', `${name}.key`, '-out', `${name}.csr`]);
    const sign = (name, issuer, extensions) => {
      writeFileSync(join(dir, `${name}.ext`), extensions);
      openssl(['x509', '-req', '-in', `${name}.csr`, '-CA', `${issuer}.crt`, '-CAkey', `${issuer}.key`, '-CAcreateserial', '-days', '1', '-sha256', '-extfile', `${name}.ext`, '-out', `${name}.crt`]);
    };
    openssl(['req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-subj', '/CN=Test Root', '-days', '1', '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-keyout', 'root.key', '-out', 'root.crt']);
    csr('miner', 'miner.example');
    sign('miner', 'root', 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:miner.example\n');
    csr('forged', 'localhost');
    sign('forged', 'miner', 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n');
    const pem = name => readFileSync(join(dir, `${name}.crt`), 'utf8');
    const rootCAs = [loadX509FromPem(pem('root'))];
    const chain = [loadX509FromPem(pem('forged')), loadX509FromPem(pem('miner'))];
    await assert.rejects(verifyServerCertificates(chain, 'localhost', rootCAs), /certificate.*failed/);
    provider = https.createServer({ key: readFileSync(join(dir, 'forged.key')), cert: pem('forged') + pem('miner'),
      minVersion: 'TLSv1.3', maxVersion: 'TLSv1.3', ciphers: SUITE }, (req, res) => {
      requests++;
      req.resume();
      req.on('end', () => { res.writeHead(200, { 'content-length': 2, connection: 'close' }); res.end('ok'); });
    });
    provider.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
    provider.on('secureConnection', socket => socket.on('data', data => { applicationBytes += data.length; }));
    const port = await listen(provider);
    const chunks = requestChunks(Buffer.from('{"input":"synthetic"}'), 'a'.repeat(64), 'abc123', { host: 'localhost', path: '/fixture' });
    await assert.rejects(exchange({ port, rootCAs, chunks, cipherSuite: SUITE }), /certificate.*failed/);
    assert.equal(requests, 0, 'credential/request reached the impersonating server');
    assert.equal(applicationBytes, 0, 'application bytes reached the impersonating server');

    // A malicious miner can trust the forged leaf locally. The independent
    // witness must still reject its captured handshake before checking a proof.
    witness = await relay(port);
    const miner = await exchange({ port: witness.port, rootCAs: [chain[0]], chunks, cipherSuite: SUITE });
    assert.equal(requests, 1);
    await assert.rejects(verifyPair({ observed: witness.observed, host: 'localhost', path: '/fixture', rootCAs,
      claim: { disclosures: encodeDisclosures(miner.disclosures) } }), /certificate.*failed/);
  } finally {
    witness?.close();
    for (const socket of sockets) socket.destroy();
    provider?.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
