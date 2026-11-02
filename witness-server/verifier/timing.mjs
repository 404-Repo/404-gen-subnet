// Diagnostic durations use Node's monotonic process clock, never security deadlines.
import {performance} from 'node:perf_hooks';

export function timing(phase, started, outcome = 'ok', duration) {
  if (process.env.WITNESS_TIMINGS !== '1') return;
  try {
    console.error(JSON.stringify({event: 'witness_timing', schema: 1, host: 'verifier',
      trace_id: `${process.pid}-process`, exchange_id: process.env.WITNESS_EXCHANGE_ID || null,
      phase: `${process.env.WITNESS_VERIFIER_MODE || 'proof'}.${phase}`,
      start_ms: duration === undefined ? started : null,
      duration_ms: duration ?? performance.now() - started, outcome}));
  } catch {} // Reporting must not affect a cryptographic decision.
}

export async function measure(phase, operation) {
  const started = performance.now();
  let outcome = 'ok';
  try { return await operation(); }
  catch (error) { outcome = 'error'; throw error; }
  finally { timing(phase, started, outcome); }
}
