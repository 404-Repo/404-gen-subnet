// Local workerd gateway forwarding requests to RunPod.
// Credentials belong to this operator process, never to test/live.py.
import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {Miniflare, convertV4MiniflareOptions} from 'miniflare';
import {runtimeOptions} from './runtime-options.mjs';

const [directory, endpoint] = process.argv.slice(2);
assert.ok(directory && endpoint, 'Usage: node test/live-gateway.mjs FIXTURE_DIR ENDPOINT_ID');
assert.match(endpoint, /^[a-zA-Z0-9_-]+$/);
assert.ok(process.env.RUNPOD_API_KEY, 'Operator RUNPOD_API_KEY is required');
const identity = JSON.parse(await readFile(join(directory, 'public.json'), 'utf8'));
const calls = JSON.parse(await readFile(join(directory, 'upstream.json'), 'utf8').catch(error => {
  if (error.code !== 'ENOENT') throw error;
  return '[]';
}));
const mf = new Miniflare(convertV4MiniflareOptions({
  host: '127.0.0.1', port: 0,
  ...runtimeOptions(),
  durableObjects: {MINERS: {className: 'MinerAdmission', useSQLite: true}},
  resourcePersistencePath: directory,
  kvNamespaces: ['REGISTRY'],
  bindings: {ROUND: String(identity.round), NETWORK: 'finney', NETUID: '17',
    ROUND_END: String(Math.floor(Date.now() / 1000) + 1800),
    RUNPOD_ENDPOINT: endpoint, RUNPOD_API_KEY: process.env.RUNPOD_API_KEY, MAX_JOBS_PER_MINER: '2'},
  outboundService: async request => {
    const url = new URL(request.url);
    assert.equal(url.origin, 'https://api.runpod.ai');
    assert.ok(url.pathname.startsWith(`/v2/${endpoint}/`));
    const response = await fetch(request.url, {method: request.method, headers: request.headers,
      body: request.method === 'POST' ? await request.text() : undefined, redirect: 'manual',
      signal: AbortSignal.timeout(10_000)});
    const data = await response.clone().json();
    // Only nonsecret operator-side test evidence is recorded.
    calls.push({method: request.method, path: url.pathname, status: response.status,
      ...(typeof data.id === 'string' ? {job_id: data.id} : {}),
      ...(typeof data.status === 'string' ? {job_status: data.status} : {}),
      ...(data.output?.queue_smoke === true ? {output: data.output} : {})});
    await writeFile(join(directory, 'upstream.json'), JSON.stringify(calls, null, 2) + '\n');
    return response;
  },
}));
const url = await mf.ready;
const kv = await mf.getKVNamespace('REGISTRY');
async function membership() {
  await kv.put('membership.json', JSON.stringify({version: 1, network: 'finney', netuid: 17,
    block: 1, fetched_at: Date.now(), block_time: Date.now(), hotkeys: identity.hotkeys}));
}
await membership();
const timer = setInterval(() => membership().catch(error => console.error(error.message)), 60_000);
await writeFile(join(directory, 'gateway-url.txt'), String(url));
console.log(JSON.stringify({url: String(url), endpoint, upstream: 'real RunPod'}));
async function close() { clearInterval(timer); await mf.dispose(); }
process.once('SIGINT', close);
process.once('SIGTERM', close);
