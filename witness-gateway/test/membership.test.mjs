import {after, before, test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash, randomUUID} from 'node:crypto';
import {Miniflare, convertV4MiniflareOptions} from 'miniflare';
import {cryptoWaitReady, encodeAddress, sr25519PairFromSeed, sr25519Sign} from '@polkadot/util-crypto';
import {runtimeOptions} from './runtime-options.mjs';

await cryptoWaitReady();
const genesis = '0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03';
const blockHash = '0x' + 'ab'.repeat(32);
const timestampKey = '0xf0c365c3cf59d671eb72da0e7a4113c49f1f0515f462cdcf84e0f1d6045dfcbb';
const countKey = '0x658faa385070e074c85bf6b568cf0555a1048e9d244171852dfe8db314dc68ca1100';
const hotkeyPrefix = '0x658faa385070e074c85bf6b568cf05559f99a2ce711f3a31b2fc05604c93f1791100';
const rpcUrl = 'https://entrypoint-finney.opentensor.ai/';
const pairs = Array.from({length: 257}, (_, index) =>
  sr25519PairFromSeed(createHash('sha256').update(`membership-${index}`).digest()));
const hotkeys = pairs.map(pair => encodeAddress(pair.publicKey, 42));
let mf, kv, mode = 'ok', registered = [0, 1], rpcCalls = [], nonceOffset = 0n;

function littleEndian(value, bytes) {
  const buffer = Buffer.alloc(bytes);
  buffer.writeUIntLE(value, 0, Math.min(6, bytes));
  return '0x' + buffer.toString('hex');
}

async function chain(request) {
  assert.equal(request.url, rpcUrl, 'Only the chain RPC may be contacted');
  const {jsonrpc, id, method, params} = await request.json();
  assert.equal(jsonrpc, '2.0');
  assert.equal(request.method, 'POST');
  rpcCalls.push({method, params});
  if (mode === 'http_error') return new Response('', {status: 503});
  if (mode === 'rpc_error') return Response.json({jsonrpc, id, error: {code: -32603}});
  if (mode === 'bad_id') return Response.json({jsonrpc, id: 2, result: genesis});
  if (mode === 'oversized') return new Response(' '.repeat(1024 * 1024 + 1));
  if (method === 'chain_getBlockHash') {
    assert.deepEqual(params, [0]);
    return Response.json({jsonrpc, id, result: mode === 'wrong_network' ? blockHash : genesis});
  }
  if (method === 'chain_getFinalizedHead') {
    assert.deepEqual(params, []);
    return Response.json({jsonrpc, id, result: blockHash});
  }
  if (method === 'chain_getHeader') {
    assert.deepEqual(params, [blockHash]);
    return Response.json({jsonrpc, id, result: {number: '0x123'}});
  }
  assert.equal(method, 'state_queryStorageAt');
  assert.equal(params[1], blockHash, 'All reads must use the finalized block');
  const keys = params[0];
  let changes;
  if (keys[0] === timestampKey) {
    assert.deepEqual(keys, [timestampKey, countKey]);
    const age = mode === 'stale' ? 301_000 : mode === 'future' ? -60_000 : 1000;
    changes = [[timestampKey, littleEndian(Date.now() - age, 8)],
      [countKey, littleEndian(mode === 'too_many' ? 4097 : registered.length, 2)]];
  } else {
    assert.ok(keys.length <= 256);
    changes = keys.map(key => {
      assert.ok(key.startsWith(hotkeyPrefix), 'Only subnet 17 keys may be read');
      assert.equal(key.length, hotkeyPrefix.length + 4);
      const uid = Buffer.from(key.slice(hotkeyPrefix.length), 'hex').readUInt16LE();
      assert.ok(uid < registered.length);
      const pair = pairs[registered[mode === 'duplicate_hotkey' ? 0 : uid]];
      return [key, '0x' + Buffer.from(pair.publicKey).toString('hex')];
    });
    if (mode === 'missing') changes.pop();
    if (mode === 'duplicate_key') changes[1] = changes[0];
    if (mode === 'wrong_key') changes[0][0] = hotkeyPrefix + 'ffff';
    if (mode === 'null_value') changes[0][1] = null;
    if (mode === 'bad_account') changes[0][1] = '0xab';
    if (mode === 'partial_second_batch' && keys.length === 1) changes = [];
  }
  // RPC storage responses may return entries in a different order.
  return Response.json({jsonrpc, id, result: [{block: mode === 'wrong_block' ? genesis : blockHash,
    changes: changes.reverse()}]});
}

function options(roundEnd = Math.floor(Date.now() / 1000) + 3600) {
  return {
    ...runtimeOptions(),
    durableObjects: {MINERS: {className: 'MinerAdmission', useSQLite: true}},
    kvNamespaces: ['REGISTRY'],
    bindings: {ROUND: '1', NETWORK: 'finney', NETUID: '17', SUBTENSOR_RPC_URL: rpcUrl,
      ROUND_END: String(roundEnd), RUNPOD_ENDPOINT: 'unused', MAX_JOBS_PER_MINER: '2'},
    outboundService: chain,
  };
}

async function refresh() {
  const worker = await mf.getWorker();
  return await worker.scheduled({cron: '* * * * *'});
}

async function status(index) {
  const hotkey = hotkeys[index], path = `/status/${randomUUID()}`;
  const nonce = String(BigInt(Date.now()) * 1_000_000n + nonceOffset++);
  const message = ['btauth/1', 'sr25519', 'GET', path,
    createHash('sha256').update('').digest('hex'), nonce, hotkey, '404-witness'].join('\n');
  const response = await mf.dispatchFetch(`https://gateway.test${path}`, {headers: {
    'x-bittensor-version': '1', 'x-bittensor-hotkey': hotkey, 'x-bittensor-receiver': '404-witness',
    'x-bittensor-nonce': nonce,
    'x-bittensor-signature': '0x' + Buffer.from(sr25519Sign(Buffer.from(message), pairs[index])).toString('hex'),
  }});
  return {status: response.status, body: await response.json()};
}

before(async () => {
  mf = new Miniflare(convertV4MiniflareOptions(options()));
  await mf.ready;
  kv = await mf.getKVNamespace('REGISTRY');
});
after(async () => { await mf?.dispose(); });

test('scheduled chain refresh controls admission as miners register and deregister', async () => {
  assert.deepEqual(await status(0), {status: 503, body: {error: 'membership_unavailable'}});
  assert.equal(rpcCalls.length, 0, 'Miner traffic must not trigger chain reads');
  assert.equal((await refresh()).outcome, 'ok');
  let snapshot = JSON.parse(await kv.get('membership.json'));
  assert.deepEqual(snapshot.hotkeys, [hotkeys[0], hotkeys[1]].sort());
  assert.equal(snapshot.network, 'finney');
  assert.equal(snapshot.netuid, 17);
  assert.equal(snapshot.block, 0x123);
  assert.equal(snapshot.block_hash, blockHash);
  assert.ok(Math.abs(snapshot.fetched_at - Date.now()) < 5000);
  assert.ok(snapshot.fetched_at - snapshot.block_time >= 1000);
  const calls = rpcCalls.length;
  assert.equal((await status(0)).status, 404, 'A member passes auth and reaches allocation lookup');
  assert.deepEqual(await status(2), {status: 403, body: {error: 'hotkey_not_registered'}});
  assert.equal(rpcCalls.length, calls);

  registered = [1, 2];
  assert.equal((await refresh()).outcome, 'ok');
  snapshot = JSON.parse(await kv.get('membership.json'));
  assert.deepEqual(snapshot.hotkeys, [hotkeys[1], hotkeys[2]].sort());
  assert.deepEqual(await status(0), {status: 403, body: {error: 'hotkey_not_registered'}});
  assert.equal((await status(2)).status, 404);
});

test('failed or incomplete refreshes preserve the last successful snapshot and its timestamps', async t => {
  const original = await kv.get('membership.json');
  for (const failure of ['http_error', 'rpc_error', 'bad_id', 'oversized', 'wrong_network',
    'wrong_block', 'stale', 'future', 'too_many', 'missing', 'duplicate_key', 'wrong_key',
    'null_value', 'bad_account', 'duplicate_hotkey']) {
    await t.test(failure, async () => {
      mode = failure;
      assert.equal((await refresh()).outcome, 'exception');
      assert.equal(await kv.get('membership.json'), original);
    });
  }
  mode = 'ok';
  assert.equal((await status(2)).status, 404, 'A brief RPC outage keeps fresh membership usable');
  const stale = {...JSON.parse(original), fetched_at: Date.now() - 300_001};
  await kv.put('membership.json', JSON.stringify(stale));
  assert.deepEqual(await status(2), {status: 503, body: {error: 'membership_unavailable'}});
  await kv.put('membership.json', JSON.stringify({...stale, fetched_at: Date.now(), block_time: Date.now() - 300_001}));
  assert.deepEqual(await status(2), {status: 503, body: {error: 'membership_unavailable'}});
});

test('larger subnets are read in bounded batches and published only when complete', async () => {
  registered = pairs.map((_, index) => index);
  const original = await kv.get('membership.json');
  mode = 'partial_second_batch';
  assert.equal((await refresh()).outcome, 'exception');
  assert.equal(await kv.get('membership.json'), original);
  mode = 'ok';
  rpcCalls = [];
  assert.equal((await refresh()).outcome, 'ok');
  assert.deepEqual(JSON.parse(await kv.get('membership.json')).hotkeys, [...hotkeys].sort());
  assert.deepEqual(rpcCalls.filter(call => call.method === 'state_queryStorageAt').map(call => call.params[0].length), [2, 256, 1]);
});

test('a refresh still runs with gateway access closed between rounds', async () => {
  await mf.setOptions(convertV4MiniflareOptions(options(0)));
  kv = await mf.getKVNamespace('REGISTRY');
  registered = [0];
  assert.equal((await refresh()).outcome, 'ok');
  assert.deepEqual(JSON.parse(await kv.get('membership.json')).hotkeys, [hotkeys[0]]);
  assert.deepEqual(await status(0), {status: 403, body: {error: 'round_closed'}});
});
