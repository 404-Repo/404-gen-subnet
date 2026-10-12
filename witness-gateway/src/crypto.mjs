import {decodeAddress, encodeAddress, sr25519Verify, xxhashAsHex} from '@polkadot/util-crypto';
import {initBridge} from '@polkadot/wasm-crypto/init';
import wasm from './crypto.wasm';

let ready;

/** The Python application uses the same Polkadot primitives as the gateway. */
export function publicKey(hotkey) {
  if (typeof hotkey !== 'string' || hotkey.length !== 48) throw new Error('Invalid hotkey');
  const key = decodeAddress(hotkey, false, 42);
  if (key.length !== 32 || encodeAddress(key, 42) !== hotkey) throw new Error('Invalid hotkey');
  return Array.from(key, byte => byte.toString(16).padStart(2, '0')).join('');
}

function bytes(hex) {
  return Uint8Array.from(hex.match(/../g), pair => parseInt(pair, 16));
}

export async function verify(message, signature, key) {
  if (!/^[0-9a-f]{128}$/.test(signature) || !/^[0-9a-f]{64}$/.test(key)) return false;
  try {
    ready ??= initBridge(async imports => ({error: null, type: 'wasm',
      wasm: new WebAssembly.Instance(wasm, {wbg: imports}).exports}));
    await ready;
    return sr25519Verify(new TextEncoder().encode(message), bytes(signature), bytes(key));
  } catch { return false; }
}

export function encodeHotkey(key) { return encodeAddress(bytes(key), 42); }

export function storageKey(pallet, name) {
  return xxhashAsHex(pallet, 128) + xxhashAsHex(name, 128).slice(2);
}
