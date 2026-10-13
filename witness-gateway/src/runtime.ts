import {Rejection} from './models';

export function nowMs(): number {return Date.now();}
export function safeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value);
}
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
export function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {status, headers: {
    'content-type': 'application/json', 'cache-control': 'no-store',
  }});
}
export function failure(error: unknown): Response {
  if (error instanceof Rejection) {
    return json({error: error.message, ...(error.exchangeId ? {exchange_id: error.exchangeId} : {})}, error.status);
  }
  return json({error: 'gateway_unavailable'}, 503);
}
export async function boundedBytes(body: ReadableStream<Uint8Array> | null, limit: number): Promise<Uint8Array> {
  if (!body) return new Uint8Array();
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > limit) throw new Rejection(413, 'body_too_large');
      chunks.push(chunk.value);
    }
  } finally {
    await reader.cancel();
  }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {result.set(chunk, offset); offset += chunk.byteLength;}
  return result;
}
export async function boundedText(body: ReadableStream<Uint8Array> | null, limit: number): Promise<string> {
  return new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(await boundedBytes(body, limit));
}
export async function sha256(value: string | Uint8Array): Promise<string> {
  const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}
export async function withStateLock<T>(ctx: DurableObjectState, operation: () => Promise<T>): Promise<T> {
  // Expected rejections must leave the gate normally so valid sockets stay open.
  let outcome: {value: T} | {error: unknown} | undefined;
  await ctx.blockConcurrencyWhile(async () => {
    try {outcome = {value: await operation()};}
    catch (error) {outcome = {error};}
  });
  if (!outcome) throw new Error('State operation did not complete');
  if ('error' in outcome) throw outcome.error;
  return outcome.value;
}
