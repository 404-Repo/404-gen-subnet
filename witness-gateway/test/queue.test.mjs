import {after, before, test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash, randomUUID} from 'node:crypto';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {setTimeout as delay} from 'node:timers/promises';
import {Miniflare, convertV4MiniflareOptions} from 'miniflare';
import {cryptoWaitReady, encodeAddress, sr25519PairFromSeed, sr25519Sign} from '@polkadot/util-crypto';
import {runtimeOptions} from './runtime-options.mjs';
import WebSocket from 'ws';

await cryptoWaitReady();
let nextSeed = 1;
function miner() {
  const seed = createHash('sha256').update(`fixture-${nextSeed++}`).digest();
  const pair = sr25519PairFromSeed(seed);
  return {pair, hotkey: encodeAddress(pair.publicKey, 42)};
}
const receiver = '404-witness';
const secret = 'SERVER_ONLY_RUNPOD_TEST_SECRET';
let mf, directory, members = [], calls = [], upstreamMode = 'ok', nonceOffset = 0n;
let remoteStatus = 'IN_QUEUE';
let unblockDispatch;
let fixedRequestTime;
let activeRound = '1', roundEnd = Math.floor(Date.now() / 1000) + 3600;

function options() {
  return {
    ...runtimeOptions(),
    durableObjects: {MINERS: {className: 'MinerAdmission', useSQLite: true}},
    resourcePersistencePath: directory,
    r2Buckets: ['TRANSCRIPTS'],
    kvNamespaces: ['REGISTRY'],
    bindings: {ROUND: activeRound, NETWORK: 'finney', NETUID: '17',
      ROUND_END: String(roundEnd), RUNPOD_ENDPOINT: 'fixture-endpoint',
      RUNPOD_API_KEY: secret, MAX_JOBS_PER_MINER: '2'},
    outboundService: async request => {
      assert.equal(new URL(request.url).origin, 'https://api.runpod.ai');
      assert.equal(request.headers.get('authorization'), `Bearer ${secret}`);
      calls.push({url: request.url, method: request.method,
        body: request.method === 'POST' ? await request.json() : null});
      if (upstreamMode === 'hold') await new Promise(resolve => { unblockDispatch = resolve; });
      if (upstreamMode === 'lost') return new Response('Connection lost after queueing', {status: 502});
      if (upstreamMode === 'malformed') return Response.json({error: secret});
      if (request.method === 'POST') return Response.json({id: `job-${calls.length}`, output: secret});
      return Response.json({status: remoteStatus, output: {secret}, error: secret});
    },
  };
}

async function membership(extra = {}) {
  const kv = await mf.getKVNamespace('REGISTRY');
  await kv.put('membership.json', JSON.stringify({version: 1, network: 'finney', netuid: 17,
    block: 100, fetched_at: Date.now(), block_time: Date.now(), hotkeys: members, ...extra}));
}

async function registeredMiner() {
  const value = miner(); members.push(value.hotkey); await membership(); return value;
}

function job(identity, changes = {}) {
  const value = {operation: 'exchange', hotkey: identity.hotkey, receiver,
    exchange_id: randomUUID(), deadline: Math.floor(Date.now() / 1000) + 600, provider: 'codex', ...changes};
  value.signature = Buffer.from(sr25519Sign(Buffer.from('404-witness-queue/v1\n' + JSON.stringify(value)), identity.pair)).toString('hex');
  return value;
}

function request(identity, method, path, value, changes = {}, recipient = receiver) {
  const body = value === undefined ? '' : Buffer.isBuffer(value) ? value : JSON.stringify(value);
  const nonce = String(BigInt(fixedRequestTime ?? Date.now()) * 1_000_000n + nonceOffset++);
  const payload = ['btauth/1', 'sr25519', method, path,
    createHash('sha256').update(body).digest('hex'), nonce, identity.hotkey, recipient].join('\n');
  return {url: `https://gateway.test${path}`, options: {method,
    ...(method !== 'GET' ? {body} : {}),
    headers: {'content-type': 'application/json', 'x-bittensor-version': '1',
      'x-bittensor-hotkey': identity.hotkey, 'x-bittensor-receiver': recipient,
      'x-bittensor-nonce': nonce, 'x-bittensor-signature': '0x' + Buffer.from(sr25519Sign(Buffer.from(payload), identity.pair)).toString('hex'), ...changes}}};
}

async function send(signed) {
  const response = await mf.dispatchFetch(signed.url, signed.options);
  const raw = await response.text();
  assert.ok(!raw.includes(secret), 'The upstream API secret must never reach the miner');
  assert.equal(response.headers.get('cache-control'), 'no-store');
  return {status: response.status, value: JSON.parse(raw), contentType: response.headers.get('content-type')};
}

before(async () => {
  directory = await mkdtemp(join(tmpdir(), '404-gateway-'));
  mf = new Miniflare(convertV4MiniflareOptions(options()));
  await mf.ready;
  await membership();
});
after(async () => { await mf?.dispose(); if (directory) await rm(directory, {recursive: true, force: true}); });

test('public endpoint requires a hotkey, not the RunPod API key', async () => {
  const before = calls.length;
  const result = await send({url: 'https://gateway.test/run', options: {method: 'POST', body: '{}'}});
  assert.equal(result.status, 401);
  assert.equal(calls.length, before);
});

test('the configured test hotkey is limited to test-0 and keeps signatures and job limits', async () => {
  const identity = miner(), outsider = miner();
  const originalRound = activeRound;
  const configuration = options();
  configuration.bindings.TEST_HOTKEY = identity.hotkey;
  await mf.setOptions(convertV4MiniflareOptions(configuration));
  try {
    const path = `/status/${randomUUID()}`;
    assert.equal((await send(request(identity, 'GET', path))).value.error, 'hotkey_not_registered');
    activeRound = 'test-0';
    configuration.bindings.ROUND = activeRound;
    await mf.setOptions(convertV4MiniflareOptions(configuration));
    const kv = await mf.getKVNamespace('REGISTRY');
    await kv.delete('membership.json');
    assert.equal((await send(request(identity, 'GET', path))).value.error, 'membership_unavailable');
    await membership({fetched_at: Date.now() - 48 * 60 * 60 * 1000 - 1});
    assert.equal((await send(request(identity, 'GET', path))).value.error, 'membership_unavailable');
    await membership({block_time: Date.now() - 48 * 60 * 60 * 1000 - 1});
    assert.equal((await send(request(identity, 'GET', path))).value.error, 'membership_unavailable');
    await membership();
    assert.equal((await send(request(outsider, 'GET', path))).value.error, 'hotkey_not_registered');
    const forged = request(identity, 'GET', path, undefined, {'x-bittensor-signature': '0x' + '00'.repeat(64)});
    assert.equal((await send(forged)).value.error, 'invalid_signature');
    for (let attempt = 0; attempt < 2; attempt++) {
      const value = job(identity);
      const signed = request(identity, 'POST', '/run', value);
      assert.equal((await send(signed)).status, 200);
      assert.equal((await send(signed)).value.error, 'hotkey_auth_replayed');
      assert.equal((await send(request(identity, 'POST', `/release/${value.exchange_id}`))).status, 200);
    }
    const before = calls.length;
    assert.equal((await send(request(identity, 'POST', '/run', job(identity)))).value.error, 'job_limit_reached');
    assert.equal(calls.length, before);
    const registered = await registeredMiner();
    assert.equal((await send(request(registered, 'POST', '/run', job(registered)))).status, 200);
    await mf.setOptions(convertV4MiniflareOptions(options()));
    assert.equal((await send(request(identity, 'GET', path))).value.error, 'hotkey_not_registered');
  } finally {
    activeRound = originalRound;
    await mf.setOptions(convertV4MiniflareOptions(options()));
  }
});

test('real sr25519 auth queues the fixed worker job and returns only safe status', async () => {
  const identity = await registeredMiner(), value = job(identity);
  const result = await send(request(identity, 'POST', '/run', value));
  assert.equal(result.status, 200);
  assert.equal(result.value.queue_state, 'queued');
  assert.equal(result.value.hotkey, identity.hotkey);
  assert.equal(result.value.exchange_id, value.exchange_id);
  assert.equal(result.value.round, activeRound);
  assert.equal(result.value.session, null);
  assertJob(value, activeRound);
  const status = await send(request(identity, 'GET', `/status/${value.exchange_id}`));
  assert.equal(status.value.queue_state, 'queued');
  assert.equal(status.value.session, null);
  assert.equal(status.value.output, undefined);
  assert.equal(status.value.runpodId, undefined);
});

test('a long queue deadline cannot extend the endpoint execution timeout', async () => {
  const previousEnd = roundEnd;
  roundEnd = Math.floor(Date.now() / 1000) + 7 * 86400;
  await mf.setOptions(convertV4MiniflareOptions(options()));
  try {
    const identity = await registeredMiner(), value = job(identity, {deadline: roundEnd});
    const result = await send(request(identity, 'POST', '/run', value));
    assert.equal(result.status, 200);
    assertJob(value, activeRound);
    const policy = calls.at(-1).body.policy;
    assert.deepEqual(Object.keys(policy), ['ttl']);
    assert.ok(policy.ttl > 6 * 86400 * 1000);
    await send(request(identity, 'POST', `/release/${value.exchange_id}`));
  } finally {
    roundEnd = previousEnd;
    await mf.setOptions(convertV4MiniflareOptions(options()));
  }
});

test('another registered miner cannot access a job', async () => {
  const owner = await registeredMiner(), other = await registeredMiner(), value = job(owner);
  await send(request(owner, 'POST', '/run', value));
  const before = calls.length;
  assert.equal((await send(request(other, 'GET', `/status/${value.exchange_id}`))).status, 404);
  assert.equal(calls.length, before);
});

test('valid signatures for another recipient are rejected before RunPod', async () => {
  const identity = await registeredMiner(), before = calls.length;
  for (const recipient of ['another-witness', identity.hotkey]) {
    const value = job(identity);
    assert.equal((await send(request(identity, 'POST', '/run', value, {}, recipient))).status, 401);
    const otherJob = job(identity, {receiver: recipient});
    assert.equal((await send(request(identity, 'POST', '/run', otherJob))).status, 400);
  }
  assert.equal(calls.length, before);
});

test('tampering, stale signatures, unregistered hotkeys and stale membership never reach RunPod', async () => {
  const identity = await registeredMiner();
  const before = calls.length;
  assert.equal((await send(request(identity, 'POST', '/run', job(identity), {'x-bittensor-signature': '0x' + '00'.repeat(64)}))).status, 401);
  assert.equal((await send(request(identity, 'POST', '/run', job(identity), {'x-bittensor-nonce': '1700000000000000000'}))).status, 401);
  const unregistered = miner();
  assert.equal((await send(request(unregistered, 'POST', '/run', job(unregistered)))).status, 403);
  for (const change of [{round: '2'}, {hotkey: unregistered.hotkey}, {receiver: unregistered.hotkey}, {deadline: 1}, {exchange_id: '../escape'}]) {
    assert.equal((await send(request(identity, 'POST', '/run', job(identity, change)))).status, 400);
  }
  const changed = job(identity); changed.deadline++;
  assert.equal((await send(request(identity, 'POST', '/run', changed))).status, 401);
  const signed = request(identity, 'POST', '/run', job(identity));
  signed.options.body += ' ';
  assert.equal((await send(signed)).status, 401);
  await membership({block_time: Date.now() - 48 * 60 * 60 * 1000 - 1});
  assert.equal((await send(request(identity, 'POST', '/run', job(identity)))).status, 503);
  await membership({netuid: 18});
  assert.equal((await send(request(identity, 'POST', '/run', job(identity)))).status, 503);
  await membership();
  assert.equal(calls.length, before);
});

test('replayed authentication is rejected and fresh retries enqueue once', async () => {
  const identity = await registeredMiner(), value = job(identity), signed = request(identity, 'POST', '/run', value);
  const before = calls.length;
  assert.equal((await send(signed)).status, 200);
  assert.equal((await send(signed)).value.error, 'hotkey_auth_replayed');
  const results = await Promise.all(Array.from({length: 5}, () => send(request(identity, 'POST', '/run', value))));
  assert.ok(results.every(result => result.value.queue_state === 'queued'));
  assert.equal(calls.length, before + 1);
  const conflict = job(identity, {exchange_id: value.exchange_id, deadline: value.deadline + 1});
  assert.equal((await send(request(identity, 'POST', '/run', conflict))).status, 409);
});

test('limits apply before starting additional jobs', async () => {
  const identity = await registeredMiner();
  for (let i = 0; i < 2; i++) {
    const value = job(identity);
    assert.equal((await send(request(identity, 'POST', '/run', value))).status, 200);
    await send(request(identity, 'POST', `/release/${value.exchange_id}`));
  }
  const before = calls.length;
  const refused = await send(request(identity, 'POST', '/run', job(identity)));
  assert.equal(refused.value.error, 'job_limit_reached');
  for (let i = 0; i < 25; i++) await send(request(identity, 'GET', `/status/${randomUUID()}`));
  assert.equal((await send(request(identity, 'GET', `/status/${randomUUID()}`))).value.error, 'request_rate_limited');
  assert.equal(calls.length, before);
});

test('one endpoint accepts both providers and the signed choice cannot be changed', async () => {
  const identity = await registeredMiner();
  for (const provider of ['api', 'codex']) {
    const value = job(identity, {provider});
    assert.equal((await send(request(identity, 'POST', '/run', value))).status, 200);
    assertJob(value, activeRound);
    const changed = {...value, provider: provider === 'api' ? 'codex' : 'api'};
    const before = calls.length;
    assert.equal((await send(request(identity, 'POST', '/run', changed))).status, 401);
    const resigned = job(identity, {exchange_id: value.exchange_id, deadline: value.deadline, provider: changed.provider});
    assert.equal((await send(request(identity, 'POST', '/run', resigned))).status, 409);
    assert.equal(calls.length, before);
    await send(request(identity, 'POST', `/release/${value.exchange_id}`));
  }
});

test('missing or unsupported providers never enqueue a worker', async () => {
  const identity = await registeredMiner(), before = calls.length;
  for (const provider of [undefined, null, '', 'API', 'https://example.org', '__proto__', ['api']]) {
    assert.equal((await send(request(identity, 'POST', '/run', job(identity, {provider})))).status, 400);
  }
  assert.equal(calls.length, before);
});

test('lost RunPod reply and gateway restart do not cause a second enqueue', async () => {
  const identity = await registeredMiner(), value = job(identity);
  const before = calls.length;
  upstreamMode = 'lost';
  const signed = request(identity, 'POST', '/run', value);
  assert.equal((await send(signed)).value.queue_state, 'uncertain');
  upstreamMode = 'ok';
  await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  assert.equal((await send(signed)).value.error, 'hotkey_auth_replayed');
  const retried = await send(request(identity, 'POST', '/run', value));
  assert.equal(retried.status, 202);
  assert.equal(retried.value.queue_state, 'uncertain');
  assert.equal(calls.length, before + 1);
});

test('malformed upstream replies are never returned to the miner', async () => {
  const identity = await registeredMiner(); upstreamMode = 'malformed';
  assert.equal((await send(request(identity, 'POST', '/run', job(identity)))).value.queue_state, 'uncertain');
  upstreamMode = 'ok';
});

test('250 different miners can queue jobs concurrently in the local Workers runtime', async () => {
  const identities = Array.from({length: 250}, () => miner());
  members.push(...identities.map(value => value.hotkey)); await membership();
  const before = calls.length;
  const started = performance.now();
  const results = await Promise.all(identities.map(identity => send(request(identity, 'POST', '/run', job(identity)))));
  assert.ok(results.every(result => result.status === 200), JSON.stringify(results.filter(result => result.status !== 200).slice(0, 3)));
  assert.equal(calls.length, before + 250);
  console.log(`250 signed queue requests: ${(performance.now() - started).toFixed(0)} ms locally; RunPod is mocked.`);
});

for (const client of ['queue', 'witness']) for (const provider of ['api', 'codex']) test(`Python ${client} client signs a ${provider} job accepted by the worker and real Workers runtime`, async () => {
  const {stdout} = await promisify(execFile)(process.env.PYTHON ?? 'python3',
    ['test/python_request.py', 'https://gateway.test', client, provider]);
  const fixture = JSON.parse(stdout);
  members.push(fixture.hotkey); await membership();
  const response = await send({url: 'https://gateway.test/run', options: {
    method: 'POST', body: fixture.body, headers: fixture.headers,
  }});
  assert.equal(response.value.queue_state, 'queued');
  assert.equal(response.value.hotkey, fixture.hotkey);
  assert.equal(calls.at(-1).body.input.provider, provider);
});

function assertJob(value, round) {
  const {token, gateway, expires, ...input} = calls.at(-1).body.input;
  assert.deepEqual(input, {...value, round});
  assert.match(token, /^[0-9a-f]{64}$/);
  assert.equal(gateway, 'https://gateway.test');
  assert.equal(expires, Math.min(value.deadline, roundEnd));
  assert.ok(calls.at(-1).body.policy.ttl <= (expires - Date.now() / 1000 + 1) * 1000);
}

async function attachWorker(value, token = calls.at(-1).body.input.token) {
  const response = await mf.dispatchFetch(`https://gateway.test/workers/${activeRound}/${value.hotkey}/${value.exchange_id}`, {
    headers: {Upgrade: 'websocket', Authorization: `Bearer ${token}`},
  });
  if (response.status !== 101) return {status: response.status, value: await response.json()};
  response.webSocket.accept();
  return {status: 101, socket: response.webSocket};
}

async function attachMiner(identity, value) {
  const signed = request(identity, 'GET', `/exchanges/${value.exchange_id}/connect`, undefined, {Upgrade: 'websocket'});
  const response = await mf.dispatchFetch(signed.url, signed.options);
  assert.equal(response.status, 101, response.status === 101 ? '' : await response.text());
  response.webSocket.accept();
  return response.webSocket;
}

async function ready(identity, value, socket) {
  socket.send(JSON.stringify({type: 'ready', session: {id: value.exchange_id}}));
  for (let i = 0; i < 30; i++) {
    const result = await send(request(identity, 'GET', `/status/${value.exchange_id}`));
    assert.equal(result.status, 200);
    assert.equal(result.contentType, 'application/json');
    assert.equal(result.value.queue_state, 'connected');
    if (result.value.session !== null) {
      assert.deepEqual(result.value.session, {id: value.exchange_id});
      return;
    }
    await delay(100);
  }
  assert.fail('Worker readiness was not saved');
}

function nextMessage(socket) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Missing WebSocket message')), 3000);
    socket.addEventListener('message', event => { clearTimeout(timer); resolve(event.data); }, {once: true});
  });
}

function closed(socket, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Socket remained open')), timeout);
    socket.addEventListener('close', () => {clearTimeout(timer); resolve();}, {once: true});
  });
}

test('only the assigned worker can claim once, and live bytes never enter R2', async () => {
  const identity = await registeredMiner(), other = await registeredMiner(), value = job(identity);
  const queued = await send(request(identity, 'POST', '/run', value));
  const token = calls.at(-1).body.input.token;
  assert.ok(!JSON.stringify(queued).includes(token));
  assert.equal((await attachWorker(value, 'f'.repeat(64))).status, 401);
  const worker = await attachWorker(value, token);
  assert.equal(worker.status, 101);
  assert.equal((await attachWorker(value, token)).status, 409);
  const starting = await send(request(identity, 'GET', `/status/${value.exchange_id}`));
  assert.equal(starting.value.queue_state, 'connected');
  assert.equal(starting.value.session, null);
  await ready(identity, value, worker.socket);
  const path = `/exchanges/${value.exchange_id}/`;
  assert.equal((await send(request(other, 'GET', `/status/${value.exchange_id}`))).status, 404);
  for (const name of ['session', 'session.json']) {
    assert.equal((await send(request(identity, 'GET', path + name))).status, 404);
  }
  const minerSocket = await attachMiner(identity, value);
  const bytes = Buffer.from([0, 255, 128, 13, 10, 42]);
  let received = nextMessage(worker.socket);
  minerSocket.send(bytes);
  assert.deepEqual(Buffer.from(await received), bytes);
  received = nextMessage(minerSocket);
  worker.socket.send(bytes);
  assert.deepEqual(Buffer.from(await received), bytes);
  const proof = JSON.stringify({type: 'handshake', claim: {fixture: true}});
  received = nextMessage(worker.socket); minerSocket.send(proof);
  assert.equal(await received, proof);
  const bucket = await mf.getR2Bucket('TRANSCRIPTS');
  assert.deepEqual((await bucket.list({prefix: `rounds/${activeRound}/${value.hotkey}/`})).objects, []);
  for (const name of ['claim.json', 'transport/client/000000', 'response.sse']) {
    assert.equal((await send(request(identity, 'PUT', path + name, bytes))).status, 404);
  }
  const done = closed(worker.socket);
  minerSocket.close(); await done;
});

test('replacement retires old workers and remains idempotent across lost replies and restart', async () => {
  const identity = await registeredMiner(), original = job(identity);
  await send(request(identity, 'POST', '/run', original));
  const firstToken = calls.at(-1).body.input.token;
  assert.equal((await send(request(identity, 'POST', '/run', job(identity)))).value.error, 'active_request_exists');
  const replacement = job(identity, {replace: original.exchange_id});
  const result = await send(request(identity, 'POST', '/run', replacement));
  assert.equal(result.status, 200);
  const secondToken = calls.at(-1).body.input.token;
  const count = calls.length;
  assert.equal((await attachWorker(original, firstToken)).status, 409);
  assert.equal((await send(request(identity, 'POST', '/run', replacement))).status, 200);
  await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  assert.equal((await send(request(identity, 'POST', '/run', replacement))).status, 200);
  assert.equal(calls.length, count);
  assert.equal((await send(request(identity, 'POST', '/run', job(identity, {replace: replacement.exchange_id})))).value.error, 'job_limit_reached');
  const worker = await attachWorker(replacement, secondToken);
  assert.equal(worker.status, 101);
  worker.socket.close();
});

test('worker disconnect and failed queued job release the allocation without resetting quota', async () => {
  const identity = await registeredMiner(), first = job(identity);
  await send(request(identity, 'POST', '/run', first));
  const worker = await attachWorker(first);
  const done = closed(worker.socket); worker.socket.close(); await done;
  const second = job(identity);
  assert.equal((await send(request(identity, 'POST', '/run', second))).status, 200);
  remoteStatus = 'FAILED';
  assert.equal((await send(request(identity, 'GET', `/status/${second.exchange_id}`))).value.queue_state, 'retired');
  remoteStatus = 'IN_QUEUE';
  assert.equal((await send(request(identity, 'POST', '/release/' + second.exchange_id))).status, 200);
  assert.equal((await send(request(identity, 'POST', '/release/' + second.exchange_id))).status, 200);
  assert.equal((await send(request(identity, 'POST', '/run', job(identity)))).value.error, 'job_limit_reached');
});

test('retired exchanges keep readable evidence with ownership checks', async () => {
  const identity = await registeredMiner(), other = await registeredMiner(), value = job(identity);
  await send(request(identity, 'POST', '/run', value));
  const path = `/exchanges/${value.exchange_id}/`;
  const missing = await send(request(identity, 'GET', path + 'progress'));
  assert.equal(missing.status, 404);
  assert.equal(missing.value.error, 'object_not_ready');
  await send(request(identity, 'POST', `/release/${value.exchange_id}`));
  const bucket = await mf.getR2Bucket('TRANSCRIPTS');
  const progress = {status: 'issued', provider_status: 200};
  await bucket.put(`rounds/${activeRound}/${value.hotkey}/${value.exchange_id}/status.json`, JSON.stringify(progress));
  const result = await send(request(identity, 'GET', path + 'progress'));
  assert.equal(result.status, 200);
  assert.equal(result.contentType, 'application/json');
  assert.deepEqual(result.value, progress);
  assert.equal((await send(request(other, 'GET', path + 'progress'))).status, 404);
  const removed = await send(request(identity, 'GET', path + 'status.json'));
  assert.equal(removed.status, 404);
  assert.equal(removed.value.error, 'unknown_route');
  await bucket.put(`rounds/${activeRound}/${value.hotkey}/${value.exchange_id}/response.sse`, 'recorded');
  const signed = request(identity, 'GET', `/exchanges/${value.exchange_id}/response.sse`);
  assert.equal(await (await mf.dispatchFetch(signed.url, signed.options)).text(), 'recorded');
  assert.equal((await send(request(other, 'GET', `/exchanges/${value.exchange_id}/response.sse`))).status, 404);
});

test('a completed manifest recovers failed, stale or missing final status without dispatching again', async () => {
  const bucket = await mf.getR2Bucket('TRANSCRIPTS');
  for (const status of ['failed', 'running', null]) {
    const identity = await registeredMiner(), other = await registeredMiner(), value = job(identity);
    await send(request(identity, 'POST', '/run', value));
    await send(request(identity, 'POST', `/release/${value.exchange_id}`));
    const prefix = `rounds/${activeRound}/${value.hotkey}/${value.exchange_id}`;
    const path = `/exchanges/${value.exchange_id}/progress`;
    if (status) await bucket.put(prefix + '/status.json', JSON.stringify({status}));
    for (const name of ['client.bin', 'server.bin', 'authorization.json', 'request.json', 'response.sse']) {
      await bucket.put(`${prefix}/${name}`, 'fixture');
    }
    const incomplete = await send(request(identity, 'GET', path));
    assert.equal(incomplete.status, status ? 200 : 404);
    assert.notEqual(incomplete.value.status, 'issued');
    const verifiedAt = Math.floor(Date.now() / 1000) - 60;
    const usage = {input_tokens: 3, output_tokens: 1, total_tokens: 4};
    await bucket.put(prefix + '/manifest.json', JSON.stringify({
      prefix, round: activeRound, hotkey: value.hotkey, exchange_id: value.exchange_id,
      verified_at: verifiedAt, provider_status: 200, provider_rejected: false, usage,
    }));
    const before = calls.length;
    const expected = {status: 'issued', authorized: true, usage, provider_status: 200,
      usage_state: 'recorded', evidence_expires: (verifiedAt + 7 * 86400) * 1000};
    for (let attempt = 0; attempt < 2; attempt++) {
      const result = await send(request(identity, 'GET', path));
      assert.equal(result.status, 200);
      assert.deepEqual(result.value, expected);
    }
    assert.equal(calls.length, before);
    assert.equal((await send(request(other, 'GET', path))).status, 404);
  }
});

test('manifest recovery preserves provider rejection and the original evidence expiry', async () => {
  const identity = await registeredMiner(), value = job(identity);
  await send(request(identity, 'POST', '/run', value));
  const bucket = await mf.getR2Bucket('TRANSCRIPTS');
  const prefix = `rounds/${activeRound}/${value.hotkey}/${value.exchange_id}`;
  const path = `/exchanges/${value.exchange_id}/progress`;
  const verifiedAt = Math.floor(Date.now() / 1000) - 8 * 86400;
  const manifest = {prefix, round: activeRound, hotkey: value.hotkey, exchange_id: value.exchange_id,
    verified_at: verifiedAt, provider_status: 401, provider_rejected: true, usage: null};
  await bucket.put(prefix + '/manifest.json', JSON.stringify(manifest));
  const result = await send(request(identity, 'GET', path));
  assert.equal(result.status, 200);
  assert.equal(result.value.provider_status, 401);
  assert.equal(result.value.usage_state, 'rejected');
  assert.equal(result.value.evidence_expires, (verifiedAt + 7 * 86400) * 1000);
  assert.ok(result.value.evidence_expires < Date.now());
  for (const changed of [{hotkey: 'wrong'}, {round: 'wrong'}, {exchange_id: randomUUID()},
    {prefix: 'wrong'}, {verified_at: null}, {verified_at: true}, {provider_status: '200'},
    {provider_rejected: null}]) {
    await bucket.put(prefix + '/manifest.json', JSON.stringify({...manifest, ...changed}));
    assert.equal((await send(request(identity, 'GET', path))).status, 503);
  }
});

test('an in-flight dispatch does not block replacement or restore its retired allocation', async () => {
  const identity = await registeredMiner(), original = job(identity);
  upstreamMode = 'hold';
  const pending = send(request(identity, 'POST', '/run', original));
  while (!unblockDispatch) await delay(10);
  const originalToken = calls.at(-1).body.input.token;
  upstreamMode = 'ok';
  const replacement = job(identity, {replace: original.exchange_id});
  const replaced = await send(request(identity, 'POST', '/run', replacement));
  assert.equal(replaced.status, 200);
  unblockDispatch(); unblockDispatch = undefined;
  assert.equal((await pending).value.queue_state, 'retired');
  assert.equal((await attachWorker(original, originalToken)).status, 409);
  assert.equal((await send(request(identity, 'GET', `/status/${replacement.exchange_id}`))).value.queue_state, 'queued');
});

test('concurrent different requests reserve one slot and identify it for release', async () => {
  const identity = await registeredMiner();
  const values = [job(identity), job(identity)];
  const before = calls.length;
  const results = await Promise.all(values.map(value => send(request(identity, 'POST', '/run', value))));
  assert.deepEqual(results.map(result => result.status).sort(), [200, 409]);
  assert.equal(calls.length, before + 1);
  const accepted = results.find(result => result.status === 200).value.exchange_id;
  assert.equal(results.find(result => result.status === 409).value.exchange_id, accepted);
});

test('a miner cannot send worker control messages or bypass the frame limit', async () => {
  for (const message of ['{"type":"finished"}', '{"type":["handshake"]}', Buffer.alloc(256 * 1024 + 1)]) {
    const identity = await registeredMiner(), value = job(identity);
    await send(request(identity, 'POST', '/run', value));
    const worker = await attachWorker(value);
    await ready(identity, value, worker.socket);
    const minerSocket = await attachMiner(identity, value);
    const disconnected = closed(worker.socket);
    minerSocket.send(message);
    await disconnected;
    assert.equal((await send(request(identity, 'GET', `/status/${value.exchange_id}`))).value.queue_state, 'retired');
  }
});

test('Python records retain identity, replay protection, capability and allowance in TypeScript', async t => {
  const identity = await registeredMiner(), value = job(identity);
  // Auth and nonce expiry must use the same clock across the runtime restart.
  // A minute-old fixture also catches accidentally falling back to wall time.
  const instant = Date.now() - 60_000;
  fixedRequestTime = instant;
  t.after(async () => {
    fixedRequestTime = undefined;
    await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
    await membership();
  });
  await membership({fetched_at: instant, block_time: instant});
  const {signature, ...payload} = value;
  const token = 'a1'.repeat(32);
  const fixture = await readFile('test/legacy-state.py', 'utf8');
  await mf.dispose();
  mf = new Miniflare(convertV4MiniflareOptions({...options(), compatibilityFlags: ['python_workers', 'python_workers_314', 'python_dedicated_snapshot'],
    modules: [{type: 'PythonModule', path: resolve('dist/gateway/legacy-state.py'), contents: fixture}]}));
  await mf.ready;
  const signed = request(identity, 'POST', '/run', value);
  const namespace = await mf.getDurableObjectNamespace('MINERS');
  const id = namespace.idFromName(`${activeRound}/${identity.hotkey}`);
  const response = await namespace.get(id).fetch('https://internal/seed', {method: 'POST', body: JSON.stringify({
    deadline: value.deadline, records: {
      [`job/${value.exchange_id}`]: {digest: createHash('sha256').update('404-witness-queue/v1\n' + JSON.stringify(payload)).digest('hex'),
        state: 'submitting', deadline: value.deadline, tokenHash: createHash('sha256').update(token).digest('hex'),
        claimed: false, minerAttached: false},
      count: 2, current: value.exchange_id, rate: {at: instant, tokens: 20},
      [`nonce/${signed.options.headers['x-bittensor-nonce']}`]: instant + 10_001,
    },
  })});
  const originalId = await response.text();
  const configuration = options();
  // Freeze the new runtime at the same instant as the Python-written records.
  configuration.modules[0].contents = (await readFile('dist/gateway/worker.mjs', 'utf8')).replaceAll('Date.now()', String(instant));
  await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(configuration)); await mf.ready;
  const restored = await mf.getDurableObjectNamespace('MINERS');
  assert.equal(restored.idFromName(`${activeRound}/${identity.hotkey}`).toString(), originalId);
  const before = calls.length;
  assert.equal((await send(signed)).value.error, 'hotkey_auth_replayed');
  fixedRequestTime = instant - 10_001;
  assert.equal((await send(request(identity, 'GET', `/status/${value.exchange_id}`))).value.error, 'stale_hotkey_auth');
  fixedRequestTime = instant;
  assert.equal((await send(request(identity, 'POST', '/run', value))).value.queue_state, 'submitting');
  assert.equal(calls.length, before);
  const worker = await attachWorker(value, token);
  assert.equal(worker.status, 101);
  await ready(identity, value, worker.socket);
  await send(request(identity, 'POST', `/release/${value.exchange_id}`));
  assert.equal((await send(request(identity, 'POST', '/run', job(identity)))).value.error, 'job_limit_reached');
});

test('hibernation restores the actual allocation sockets and their message limits', async () => {
  const configuration = options();
  const probe = await readFile('test/worker-probe.mjs', 'utf8');
  configuration.modules = [{type: 'ESModule', path: resolve('dist/gateway/probe.mjs'), contents: probe}, ...configuration.modules];
  await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(configuration)); await mf.ready;
  try {
    const identity = await registeredMiner(), value = job(identity);
    await send(request(identity, 'POST', '/run', value));
    const worker = await attachWorker(value);
    await ready(identity, value, worker.socket);
    const minerSocket = await attachMiner(identity, value);
    const namespace = await mf.getDurableObjectNamespace('MINERS');
    const object = namespace.get(namespace.idFromName(`${activeRound}/${identity.hotkey}`));
    const inspect = async method => (await object.fetch('https://internal/test-state', {
      method, headers: {'x-test-id': value.exchange_id},
    })).json();
    const before = await inspect('POST');
    await delay(22_000);
    const after = await inspect('GET');
    assert.notEqual(after.instance, before.instance);
    assert.deepEqual(after.connections, before.connections);
    const received = nextMessage(worker.socket);
    minerSocket.send(Buffer.from([0, 255, 128]));
    assert.deepEqual(Buffer.from(await received), Buffer.from([0, 255, 128]));
    const disconnected = closed(worker.socket);
    minerSocket.send(Buffer.from([1]));
    await disconnected;
    assert.equal((await send(request(identity, 'GET', `/status/${value.exchange_id}`))).value.queue_state, 'retired');
  } finally {
    await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  }
});

test('deadline alarm retires an idle allocation and releases its server sockets', async () => {
  const configuration = options();
  const probe = await readFile('test/worker-probe.mjs', 'utf8');
  configuration.modules = [{type: 'ESModule', path: resolve('dist/gateway/probe.mjs'), contents: probe}, ...configuration.modules];
  await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(configuration)); await mf.ready;
  let worker, minerSocket;
  try {
    const identity = await registeredMiner(), value = job(identity, {deadline: Math.floor(Date.now() / 1000) + 5});
    await send(request(identity, 'POST', '/run', value));
    const base = String(await mf.ready).replace(/^http/, 'ws').replace(/\/$/, '');
    const open = (path, headers) => new Promise((resolve, reject) => {
      const socket = new WebSocket(base + path, {headers});
      socket.once('open', () => resolve(socket)); socket.once('error', reject);
    });
    worker = await open(`/workers/${activeRound}/${identity.hotkey}/${value.exchange_id}`,
      {Authorization: `Bearer ${calls.at(-1).body.input.token}`});
    await ready(identity, value, worker);
    const path = `/exchanges/${value.exchange_id}/connect`;
    minerSocket = await open(path, request(identity, 'GET', path).options.headers);
    await closed(worker, 10000);
    // Pinned Miniflare delays miner-side close delivery in the original JS gateway too.
    const namespace = await mf.getDurableObjectNamespace('MINERS');
    const object = namespace.get(namespace.idFromName(`${activeRound}/${identity.hotkey}`));
    const state = await (await object.fetch('https://internal/test-state', {
      headers: {'x-test-id': value.exchange_id},
    })).json();
    assert.deepEqual(state.connections, []);
    assert.equal((await send(request(identity, 'GET', `/status/${value.exchange_id}`))).value.queue_state, 'retired');
    assert.equal((await send(request(identity, 'POST', '/run', job(identity)))).status, 200);
  } finally {
    worker?.terminate(); minerSocket?.terminate();
    await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  }
});

test('a runtime interruption during enqueue leaves a recoverable submitting allocation', async () => {
  const identity = await registeredMiner(), value = job(identity), before = calls.length;
  upstreamMode = 'hold';
  const pending = send(request(identity, 'POST', '/run', value)).catch(() => null);
  while (!unblockDispatch) await delay(10);
  const token = calls.at(-1).body.input.token;
  await mf.dispose();
  unblockDispatch(); unblockDispatch = undefined; upstreamMode = 'ok';
  await pending;
  mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  assert.equal((await send(request(identity, 'POST', '/run', value))).value.queue_state, 'submitting');
  assert.equal(calls.length, before + 1);
  const worker = await attachWorker(value, token);
  assert.equal(worker.status, 101);
  worker.socket.close();
});

test('malformed signatures, unsupported crypto and unsafe numeric jobs fail closed', async () => {
  const identity = await registeredMiner(), before = calls.length;
  for (const changes of [
    {'x-bittensor-signature': '0x00'}, {'x-bittensor-signature': '0x' + 'aa'.repeat(64) + '\n'}, {'x-bittensor-signature': '0x' + 'FF'.repeat(64)},
    {'x-bittensor-crypto': 'ed25519'}, {'x-bittensor-hotkey': identity.hotkey.slice(0, -1) + '0'},
  ]) assert.equal((await send(request(identity, 'POST', '/run', job(identity), changes))).status, 401, JSON.stringify(changes));
  for (const deadline of ['true', '1' + '0'.repeat(500), '1e500', 'NaN']) {
    const raw = Buffer.from(JSON.stringify(job(identity)).replace(/"deadline":\d+/, `"deadline":${deadline}`));
    assert.equal((await send(request(identity, 'POST', '/run', raw))).status, 400);
  }
  for (const changes of [{exchange_id: randomUUID() + '\n'}, {replace: randomUUID() + '\n'}]) {
    assert.equal((await send(request(identity, 'POST', '/run', job(identity, changes)))).status, 400);
  }
  const bom = Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(JSON.stringify(job(identity)))]);
  assert.equal((await send(request(identity, 'POST', '/run', bom))).status, 400);
  const trailingSignature = job(identity); trailingSignature.signature += '\n';
  assert.equal((await send(request(identity, 'POST', '/run', trailingSignature))).status, 400);
  await membership({version: true});
  assert.equal((await send(request(identity, 'POST', '/run', job(identity)))).status, 503);
  await membership();
  assert.equal(calls.length, before);
});

test('gateway assigns its active round and rejects miner-supplied rounds', async () => {
  const identity = await registeredMiner();
  const before = calls.length;
  for (const round of ['1', '2', '../escape']) {
    const result = await send(request(identity, 'POST', '/run', job(identity, {round})));
    assert.equal(result.status, 400);
    assert.equal(result.value.error, 'invalid_job');
  }
  assert.equal(calls.length, before);
  activeRound = '42';
  await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  try {
    const value = job(identity);
    assert.equal(Object.hasOwn(value, 'round'), false);
    const result = await send(request(identity, 'POST', '/run', value));
    assert.equal(result.status, 200);
    assert.equal(result.value.round, '42');
    assertJob(value, '42');
  } finally {
    activeRound = '1';
    await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  }
});

test('queueing rejects a signed body that finishes uploading after phase closure', async () => {
  const identity = await registeredMiner();
  roundEnd = Math.floor(Date.now() / 1000) + 15;
  await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  try {
    const signed = request(identity, 'POST', '/run', job(identity));
    const bytes = Buffer.from(signed.options.body);
    const before = calls.length;
    assert.ok(Date.now() < roundEnd * 1000 - 1000, 'Runtime must be ready before the phase closes');
    signed.options.body = new ReadableStream({
      async start(controller) {
        controller.enqueue(bytes.subarray(0, 1));
        await delay(roundEnd * 1000 - Date.now() + 100);
        controller.enqueue(bytes.subarray(1));
        controller.close();
      },
    });
    signed.options.duplex = 'half';
    const result = await send(signed);
    assert.equal(result.status, 403);
    assert.equal(result.value.error, 'round_closed');
    assert.equal(calls.length, before);
  } finally {
    roundEnd = Math.floor(Date.now() / 1000) + 3600;
    await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  }
});

test('closed phase blocks queueing, retries, status, downloads and uploads', async () => {
  const identity = await registeredMiner(), value = job(identity);
  await send(request(identity, 'POST', '/run', value));
  const before = calls.length;
  roundEnd = 0;
  await mf.dispose(); mf = new Miniflare(convertV4MiniflareOptions(options())); await mf.ready;
  const path = `/exchanges/${value.exchange_id}/`;
  for (const [method, route, body] of [
    ['POST', '/run', job(identity)], ['POST', '/run', value],
    ['GET', `/status/${value.exchange_id}`], ['GET', path + 'progress'],
    ['GET', path + 'response.sse'], ['PUT', path + 'transport/client/000000', Buffer.from('bytes')],
  ]) {
    const result = await send(request(identity, method, route, body));
    assert.equal(result.status, 403);
    assert.equal(result.value.error, 'round_closed');
  }
  assert.equal(calls.length, before);
  const bucket = await mf.getR2Bucket('TRANSCRIPTS');
  assert.equal(await bucket.get(`rounds/1/${identity.hotkey}/${value.exchange_id}/transport/client/000000`), null);
});
