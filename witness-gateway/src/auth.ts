import {publicKey, verify} from './crypto.mjs';
import {Rejection, type Exchange, type GatewayEnv, type GatewayRequest} from './models';
import {boundedBytes, isRecord, nowMs, safeInteger, sha256} from './runtime';

export const WITNESS_ID = '404-witness';
const MEMBERSHIP_MAX_AGE_MS = 48 * 60 * 60 * 1000;
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/;

export function exchangePayload(job: Exchange): string {
  const fields = {operation: job.operation, hotkey: job.hotkey, receiver: job.receiver,
    exchange_id: job.exchange_id, deadline: job.deadline, provider: job.provider,
    ...(job.replace ? {replace: job.replace} : {})};
  return '404-witness-queue/v1\n' + JSON.stringify(fields);
}

async function registered(hotkey: string, env: GatewayEnv): Promise<void> {
  const raw = await env.REGISTRY.get('membership.json');
  let snapshot: unknown;
  try {snapshot = raw && raw.length <= 512 * 1024 ? JSON.parse(raw) : null;}
  catch {throw new Rejection(503, 'membership_unavailable');}
  const now = nowMs();
  if (!isRecord(snapshot) || !safeInteger(snapshot.version) || snapshot.version !== 1
    || snapshot.network !== env.NETWORK || snapshot.netuid !== Number(env.NETUID)
    || !safeInteger(snapshot.block) || snapshot.block < 0 || !Array.isArray(snapshot.hotkeys)
    || snapshot.hotkeys.length > 4096) throw new Rejection(503, 'membership_unavailable');
  for (const name of ['fetched_at', 'block_time']) {
    const value = snapshot[name];
    if (!safeInteger(value) || value > now + 30_000 || now - value > MEMBERSHIP_MAX_AGE_MS) {
      throw new Rejection(503, 'membership_unavailable');
    }
  }
  if (env.ROUND === 'test-0' && hotkey === env.TEST_HOTKEY) return;
  if (!snapshot.hotkeys.includes(hotkey)) throw new Rejection(403, 'hotkey_not_registered');
}

export async function authenticate(request: Request, env: GatewayEnv): Promise<GatewayRequest> {
  const url = new URL(request.url), path = url.pathname;
  const status = request.method === 'GET' ? /^\/status\/([0-9a-f-]{36})(?![\s\S])/.exec(path) : null;
  const objectRoute = request.method === 'GET'
    ? /^\/exchanges\/([0-9a-f-]{36})\/(connect|progress|response\.sse)(?![\s\S])/.exec(path) : null;
  const release = request.method === 'POST' ? /^\/release\/([0-9a-f-]{36})(?![\s\S])/.exec(path) : null;
  const route = status || objectRoute || release;
  if (url.search || !(request.method === 'POST' && path === '/run' || route && UUID.test(route[1]))) {
    throw new Rejection(404, 'unknown_route');
  }
  const headers = request.headers, hotkey = headers.get('x-bittensor-hotkey') || '';
  let key: string;
  try {key = publicKey(hotkey);}
  catch {throw new Rejection(401, 'invalid_hotkey');}
  const nonce = headers.get('x-bittensor-nonce') || '';
  const signature = headers.get('x-bittensor-signature') || '';
  const algorithm = headers.get('x-bittensor-crypto');
  if (headers.get('x-bittensor-version') !== '1' || headers.get('x-bittensor-receiver') !== WITNESS_ID
    || algorithm !== null && algorithm !== 'sr25519' || !/^[1-9][0-9]{15,19}(?![\s\S])/.test(nonce)
    || !/^0x[0-9a-f]{128}(?![\s\S])/.test(signature)) throw new Rejection(401, 'invalid_hotkey_auth');
  const age = BigInt(nowMs()) * 1_000_000n - BigInt(nonce);
  if (age > 10_000_000_000n || age < -2_000_000_000n) throw new Rejection(401, 'stale_hotkey_auth');
  const body = await boundedBytes(request.body, request.method === 'POST' && !release ? 4096 : 0);
  const message = ['btauth/1', 'sr25519', request.method, path, await sha256(body), nonce, hotkey, WITNESS_ID].join('\n');
  if (!await verify(message, signature.slice(2), key)) throw new Rejection(401, 'invalid_signature');
  await registered(hotkey, env);
  if (route) {
    return {hotkey, nonce, requestId: route[1],
      ...(objectRoute?.[2] === 'connect' ? {connect: true} : {}),
      ...(objectRoute && objectRoute[2] !== 'connect'
        ? {object: objectRoute[2] === 'progress' ? 'status.json' : 'response.sse'} : {}),
      ...(release ? {release: true} : {})};
  }
  let data: unknown;
  try {data = JSON.parse(new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(body));}
  catch {throw new Rejection(400, 'invalid_job');}
  const required = ['operation', 'hotkey', 'receiver', 'exchange_id', 'deadline', 'provider', 'signature'];
  if (!isRecord(data) || required.some(name => !Object.hasOwn(data, name))
    || Object.keys(data).some(name => name !== 'replace' && !required.includes(name))) {
    throw new Rejection(400, 'invalid_job');
  }
  if (Object.hasOwn(data, 'replace') && (typeof data.replace !== 'string'
    || !UUID.test(data.replace) || data.replace === data.exchange_id)) throw new Rejection(400, 'invalid_job');
  if (data.operation !== 'exchange' || data.hotkey !== hotkey || data.receiver !== WITNESS_ID
    || data.provider !== 'api' && data.provider !== 'codex' || typeof data.exchange_id !== 'string'
    || !UUID.test(data.exchange_id) || !safeInteger(data.deadline) || data.deadline <= nowMs() / 1000
    || data.deadline > nowMs() / 1000 + 604801 || typeof data.signature !== 'string'
    || !/^[0-9a-f]{128}(?![\s\S])/.test(data.signature)) throw new Rejection(400, 'invalid_job');
  const job: Exchange = {operation: 'exchange', hotkey, receiver: WITNESS_ID,
    exchange_id: data.exchange_id, deadline: data.deadline, provider: data.provider, signature: data.signature,
    ...(typeof data.replace === 'string' ? {replace: data.replace} : {})};
  if (!await verify(exchangePayload(job), job.signature, key)) throw new Rejection(401, 'invalid_signature');
  return {hotkey, nonce, requestId: job.exchange_id, exchange: job, origin: url.origin};
}
