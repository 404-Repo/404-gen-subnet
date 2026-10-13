/** Publish complete registration snapshots from one finalized Finney block. */
import {encodeHotkey, storageKey} from './crypto.mjs';
import type {Membership} from './models';
import {boundedText, isRecord, nowMs, safeInteger} from './runtime';

const FINNEY_GENESIS = '0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03';
function record(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) throw new Error('Invalid chain reply');
  return value;
}
function hexBytes(value: unknown, length: number): string {
  if (typeof value !== 'string' || !new RegExp(`^0x[0-9a-f]{${length * 2}}(?![\\s\\S])`).test(value)) throw new Error('Invalid chain value');
  return value.slice(2);
}
function littleEndian(value: unknown, length: number): number {
  const hex = hexBytes(value, length);
  let integer = 0n;
  for (let index = length - 1; index >= 0; index--) integer = integer * 256n + BigInt(parseInt(hex.slice(index * 2, index * 2 + 2), 16));
  const number = Number(integer);
  if (!safeInteger(number)) throw new Error('Chain value exceeds integer precision');
  return number;
}
function u16(value: number): string {
  return (value & 255).toString(16).padStart(2, '0') + (value >>> 8).toString(16).padStart(2, '0');
}
class SubtensorRpc {
  constructor(readonly url: string, readonly signal: AbortSignal) {}
  async call(method: string, params: unknown[] = []): Promise<unknown> {
    const response = await fetch(this.url, {method: 'POST', redirect: 'manual', signal: this.signal,
      headers: {'content-type': 'application/json'}, body: JSON.stringify({jsonrpc: '2.0', id: 1, method, params})});
    if (!response.ok) {await response.body?.cancel(); throw new Error(`Chain RPC HTTP ${response.status}`);}
    const reply = record(JSON.parse(await boundedText(response.body, 1024 * 1024)));
    if (reply.jsonrpc !== '2.0' || !safeInteger(reply.id) || reply.id !== 1
      || 'error' in reply || !('result' in reply)) throw new Error('Chain RPC failed');
    return reply.result;
  }
  async storage(keys: string[], block: string): Promise<string[]> {
    const result = await this.call('state_queryStorageAt', [keys, block]);
    if (!Array.isArray(result) || result.length !== 1) throw new Error('Invalid chain storage reply');
    const entry = record(result[0]), changes = entry.changes;
    if (entry.block !== block || !Array.isArray(changes) || changes.length !== keys.length) throw new Error('Incomplete chain storage reply');
    const expected = new Set(keys), values = new Map<string, string>();
    for (const change of changes) {
      if (!Array.isArray(change) || change.length !== 2 || typeof change[0] !== 'string'
        || typeof change[1] !== 'string' || !expected.has(change[0]) || values.has(change[0])) throw new Error('Invalid chain storage entry');
      values.set(change[0], change[1]);
    }
    return keys.map(key => values.get(key)!);
  }
}
async function readMembership(env: MembershipBindings): Promise<Membership> {
  const netuid = Number(env.NETUID);
  if (env.NETWORK !== 'finney' || !/^[+-]?\d+(?![\s\S])/.test(env.NETUID.trim()) || !safeInteger(netuid)
    || netuid < 0 || netuid > 65535 || new URL(env.SUBTENSOR_RPC_URL).protocol !== 'https:') throw new Error('Invalid membership configuration');
  const rpc = new SubtensorRpc(env.SUBTENSOR_RPC_URL, AbortSignal.timeout(45_000));
  if (await rpc.call('chain_getBlockHash', [0]) !== FINNEY_GENESIS) throw new Error('RPC is not connected to Finney');
  const finalized = await rpc.call('chain_getFinalizedHead');
  hexBytes(finalized, 32);
  const blockHash = finalized as string;
  const header = await rpc.call('chain_getHeader', [blockHash]);
  const values = await rpc.storage([storageKey('Timestamp', 'Now'),
    storageKey('SubtensorModule', 'SubnetworkN') + u16(netuid)], blockHash);
  const height = record(header).number;
  if (typeof height !== 'string' || !/^0x[0-9a-f]+(?![\s\S])/.test(height)) throw new Error('Invalid block height');
  const block = Number(BigInt(height)), blockTime = littleEndian(values[0], 8), count = littleEndian(values[1], 2);
  if (!safeInteger(block) || count > 4096) throw new Error('Invalid membership size or block');
  const prefix = storageKey('SubtensorModule', 'Keys') + u16(netuid), hotkeys: string[] = [];
  for (let start = 0; start < count; start += 256) {
    const keys = Array.from({length: Math.min(256, count - start)}, (_, offset) => prefix + u16(start + offset));
    for (const key of await rpc.storage(keys, blockHash)) hotkeys.push(encodeHotkey(hexBytes(key, 32)));
  }
  const now = nowMs();
  if (blockTime > now + 30_000 || now - blockTime > 300_000) throw new Error('Finalized block is stale');
  if (new Set(hotkeys).size !== count) throw new Error('Duplicate registered hotkeys');
  return {version: 1, network: 'finney', netuid, block, block_hash: blockHash,
    block_time: blockTime, fetched_at: now, hotkeys: hotkeys.sort()};
}
export default {
  async scheduled(_controller: ScheduledController, env: MembershipBindings): Promise<void> {
    try {
      const snapshot = await readMembership(env);
      await env.REGISTRY.put('membership.json', JSON.stringify(snapshot));
      console.log(JSON.stringify({event: 'membership_refreshed', netuid: snapshot.netuid,
        block: snapshot.block, members: snapshot.hotkeys.length}));
    } catch (error) {
      console.log(JSON.stringify({event: 'membership_refresh_failed', network: env.NETWORK,
        netuid: env.NETUID, error: error instanceof Error ? error.message : String(error)}));
      // Keep the saved snapshot untouched and retry on the next cron tick.
    }
  },
} satisfies ExportedHandler<MembershipBindings>;
