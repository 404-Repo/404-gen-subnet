// Run only inside the witness worker. Cryptographic decisions stay in the shared verifier.
import { readFileSync, writeFileSync, accessSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { authorizeHandshake, authorizeRequest, finishAuthorizedResponse } from '../verifier/verify.mjs';
import { ClientHelloGate, responseUsage, providerRejected } from '../verifier/gate.mjs';
import { DEFAULT_BINARY } from '../verifier/worker.mjs';
import { measure, timing } from '../verifier/timing.mjs';

timing('process_start_to_ready', 0);

export async function run(mode, directory, params, provider, overrides = {}) {
  const targets = {
    codex: {host: 'chatgpt.com', path: '/backend-api/codex/responses', authentication: 'subscription'},
    api: {host: 'api.openai.com', path: '/v1/responses', authentication: 'api'},
  };
  if (mode === 'health') {
    accessSync(DEFAULT_BINARY);
    accessSync(join(params, 'verifying.bin'));
    return { status: 'ready' };
  }
  if (provider !== 'api' && provider !== 'codex') throw new Error('Unknown witness provider');
  const target = targets[provider];
  if (mode === 'hello') {
    const gate = new ClientHelloGate();
    if (!gate.receive(readFileSync(join(directory, 'client.bin')))) throw new Error('Incomplete ClientHello');
    return { released: gate.released };
  }
  if (mode === 'handshake' || mode === 'authorize') {
    const claim = JSON.parse(readFileSync(join(directory, mode === 'handshake' ? 'handshake-claim.json' : 'claim.json'), 'utf8'));
    if (mode === 'authorize') {
      const handshake = JSON.parse(readFileSync(join(directory, 'handshake.json'), 'utf8'));
      const a = claim.response_key, b = handshake.responseKey;
      if (a?.key !== b.key || a?.iv !== b.iv || a?.number !== b.number) throw new Error('Response key changed after handshake');
    }
    const authorization = await measure('verification', () => (mode === 'handshake' ? authorizeHandshake : authorizeRequest)({
      observed: { client: [readFileSync(join(directory, 'client.bin'))], server: [readFileSync(join(directory, 'server.bin'))] },
      claim,
      ...target,
      proofOptions: { params }, ...overrides,
    }));
    if (mode === 'handshake') {
      writeFileSync(join(directory, 'handshake.json'), JSON.stringify(authorization));
      return { handshake: true };
    }
    writeFileSync(join(directory, 'request.json'), authorization.requestBody);
    writeFileSync(join(directory, 'authorization.json'), JSON.stringify({
      ...authorization, requestBody: authorization.requestBody.toString('base64'),
    }));
    return { authorized: true };
  }
  if (mode !== 'finish') throw new Error('Unknown verifier mode');
  const saved = JSON.parse(readFileSync(join(directory, 'authorization.json'), 'utf8'));
  const result = finishAuthorizedResponse([readFileSync(join(directory, 'server.bin'))], {
    ...saved, requestBody: Buffer.from(saved.requestBody, 'base64'),
  });
  writeFileSync(join(directory, 'response.sse'), result.responseBody);
  return { request_sha256: result.request_sha256, response_sha256: result.response_sha256,
    provider_status: result.responseStatus, provider_rejected: providerRejected(result.responseStatus),
    usage: result.responseStatus === 200 ? responseUsage(result.responseBody) : null };
}

if (import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  console.log(JSON.stringify(await run(...process.argv.slice(2))));
}
