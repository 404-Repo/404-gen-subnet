/** I/O durations only: Cloudflare freezes its monotonic clock between I/O events. */
export function timing(exchangeId: string | null, phase: string, started: number, outcome = 'ok'): void {
  try {
    console.log(JSON.stringify({event: 'witness_timing', schema: 1, host: 'gateway',
      exchange_id: exchangeId, phase, duration_ms: performance.now() - started,
      clock: 'cloudflare_io', outcome}));
  } catch {} // Diagnostics cannot change admission, dispatch or recovery.
}

export async function measure<T>(exchangeId: string, phase: string, operation: () => Promise<T>): Promise<T> {
  const started = performance.now();
  let outcome = 'ok';
  try {return await operation();}
  catch (error) {outcome = 'error'; throw error;}
  finally {timing(exchangeId, phase, started, outcome);}
}
