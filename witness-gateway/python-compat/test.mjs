import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
import {Miniflare} from 'miniflare';
import {cryptoWaitReady, encodeAddress, sr25519PairFromSeed, sr25519Sign} from '@polkadot/util-crypto';

await cryptoWaitReady();
const root = fileURLToPath(new URL('./', import.meta.url));
const mf = new Miniflare({
  modulesRoot: root,
  modules: [{type: 'PythonModule', path: `${root}worker.py`},
    {type: 'ESModule', path: `${root}dist/crypto.mjs`},
    {type: 'CompiledWasm', path: `${root}dist/crypto.wasm`}],
  compatibilityDate: '2025-10-11', compatibilityFlags: ['python_workers'],
  durableObjects: {RELAY: {className: 'Relay', useSQLite: true}},
});
const pair = sr25519PairFromSeed(createHash('sha256').update('python-compat-fixture').digest());
const hotkey = encodeAddress(pair.publicKey, 42);
let offset = 0n;
function headers(path, changes = {}) {
  const nonce = String(BigInt(Date.now()) * 1_000_000n + offset++);
  const message = ['btauth/1', 'sr25519', 'GET', path,
    createHash('sha256').update('').digest('hex'), nonce, hotkey, '404-witness'].join('\n');
  return {'x-bittensor-hotkey': hotkey, 'x-bittensor-nonce': nonce,
    'x-bittensor-signature': '0x' + Buffer.from(sr25519Sign(Buffer.from(message), pair)).toString('hex'), ...changes};
}
function nextMessage(socket) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Missing relay message')), 5000);
    socket.addEventListener('message', event => {clearTimeout(timer); resolve(event.data);}, {once: true});
  });
}
try {
  await mf.ready;
  const valid = await mf.dispatchFetch('https://test/verify', {headers: headers('/verify')});
  assert.equal(valid.status, 200, await valid.text());
  for (const signature of ['0x' + '00'.repeat(64), '0x00', '0x' + 'ff'.repeat(64)]) {
    const invalid = await mf.dispatchFetch('https://test/verify', {
      headers: headers('/verify', {'x-bittensor-signature': signature}),
    });
    assert.equal(invalid.status, 401, await invalid.text());
  }
  const sockets = [];
  for (const role of ['miner', 'worker']) {
    const path = `/${role}`;
    const response = await mf.dispatchFetch(`https://test${path}`, {headers: headers(path, {Upgrade: 'websocket'})});
    assert.equal(response.status, 101, response.status === 101 ? '' : await response.text());
    response.webSocket.accept(); sockets.push(response.webSocket);
  }
  for (const [sender, receiver] of [sockets, [...sockets].reverse()]) {
    const bytes = Buffer.alloc(256 * 1024);
    for (let i = 0; i < bytes.length; i++) bytes[i] = i % 256;
    const received = nextMessage(receiver);
    sender.send(bytes);
    assert.deepEqual(Buffer.from(await received), bytes);
    const text = nextMessage(receiver);
    sender.send('{"type":"handshake","claim":{"fixture":true}}');
    assert.equal(await text, '{"type":"handshake","claim":{"fixture":true}}');
  }
  const state = async () => (await mf.dispatchFetch('https://test/state', {headers: headers('/state')})).json();
  const before = await state();
  assert.deepEqual(before.attachments.map(value => value.messages), [2, 2]);
  await delay(12_000);
  const after = await state();
  assert.notEqual(after.instance, before.instance, 'The Durable Object must actually hibernate');
  assert.deepEqual(after.attachments, before.attachments);
  const afterWake = nextMessage(sockets[1]);
  sockets[0].send(Buffer.from([0, 255, 128]));
  assert.deepEqual(Buffer.from(await afterWake), Buffer.from([0, 255, 128]));
  sockets.forEach(socket => socket.close());
  console.log('Python workerd: sr25519, malformed signatures, binary/text relay, hibernation and attachments passed.');
} finally { await mf.dispose(); }
