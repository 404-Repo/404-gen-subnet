export function publicKey(hotkey: string): string;
export function verify(message: string, signature: string, key: string): Promise<boolean>;
export function encodeHotkey(key: string): string;
export function storageKey(pallet: string, name: string): string;
