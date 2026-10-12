import {decodeAddress, encodeAddress, sr25519Verify} from '@polkadot/util-crypto';
import {initBridge} from '@polkadot/wasm-crypto/init';
import wasm from './crypto.wasm';

let ready;

/** Reuse the existing sr25519 implementation and canonical SS58 validation. */
export async function verify(message, signature, hotkey) {
  if (typeof hotkey !== 'string' || hotkey.length !== 48
    || typeof signature !== 'string' || !/^[0-9a-f]{128}$/.test(signature)) return false;
  try {
    ready ??= initBridge(async imports => ({error: null, type: 'wasm',
      wasm: new WebAssembly.Instance(wasm, {wbg: imports}).exports}));
    await ready;
    const key = decodeAddress(hotkey, false, 42);
    if (key.length !== 32 || encodeAddress(key, 42) !== hotkey) return false;
    const bytes = Uint8Array.from(signature.match(/../g), pair => parseInt(pair, 16));
    return sr25519Verify(new TextEncoder().encode(message), bytes, key);
  } catch { return false; }
}
