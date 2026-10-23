/** Bounded, JSON-safe diagnostics shared by the worker and HTTP gate. */
import { Buffer } from 'node:buffer';

export const MAX_FAILURES = 50;
export const MAX_DETAIL_BYTES = 2048;
export const MAX_REPORT_BYTES = 64 * 1024;

const stringify = JSON.stringify;
const setPrototypeOf = Object.setPrototypeOf;
const hasOwn = Object.hasOwn;
const slice = Function.prototype.call.bind(String.prototype.slice);
const bufferToString = Function.prototype.call.bind(Buffer.prototype.toString);
const bufferFrom = Buffer.from;
const byteLength = Buffer.byteLength;
const min = Math.min;
const isFinite = Number.isFinite;

// Miner code can replace JSON.stringify and install inherited toJSON hooks.
// Every container in a report must be independent of those hooks.
function withoutPrototype(value) {
  return setPrototypeOf(value, null);
}

function boundedText(text) {
  if (typeof text !== 'string') return { text: 'No error detail available', truncated: true };
  // Slice first so an oversized exception message cannot allocate another
  // equally large buffer merely to produce its bounded diagnostic.
  const bytes = bufferFrom(slice(text, 0, MAX_DETAIL_BYTES), 'utf8');
  let end = min(bytes.length, MAX_DETAIL_BYTES);
  while (end < bytes.length && (bytes[end] & 0xc0) === 0x80) end--;
  const bounded = bufferToString(bytes, 'utf8', 0, end);
  return { text: bounded, truncated: bounded !== text };
}

function boundReport(result, error) {
  let truncated = hasOwn(result, 'truncated') && !!result.truncated;
  const number = (value) => {
    if (typeof value === 'number' && isFinite(value)) return value;
    truncated = true;
    return null;
  };
  const vector = (value) => withoutPrototype({ x: number(value?.x), y: number(value?.y), z: number(value?.z) });
  const metrics = result.metrics == null ? null : withoutPrototype({
    vertices: number(result.metrics.vertices),
    drawCalls: number(result.metrics.drawCalls),
    depth: number(result.metrics.depth),
    instances: number(result.metrics.instances),
    textureBytes: number(result.metrics.textureBytes),
    bbox: result.metrics.bbox == null ? null : withoutPrototype({
      min: vector(result.metrics.bbox.min),
      max: vector(result.metrics.bbox.max),
    }),
  });
  const report = withoutPrototype({ passed: result.passed, failures: withoutPrototype([]), metrics, truncated });
  if (error !== undefined) {
    const label = boundedText(error);
    report.error = label.text;
    report.truncated ||= label.truncated;
  }

  for (let i = 0; i < result.failures.length; i++) {
    if (report.failures.length === MAX_FAILURES) {
      report.truncated = true;
      break;
    }
    const failure = result.failures[i];
    const detail = boundedText(failure.detail);
    const item = withoutPrototype({ stage: failure.stage, rule: failure.rule, detail: detail.text });
    report.truncated ||= detail.truncated || truncated;
    report.failures[report.failures.length] = item;
    if (byteLength(stringify(report), 'utf8') > MAX_REPORT_BYTES) {
      report.failures.length--;
      report.truncated = true;
      break;
    }
  }
  return report;
}

export function boundValidationResult(result) {
  return boundReport(result);
}

export function boundValidationError(error, result) {
  const report = boundReport(result, error);
  return withoutPrototype({
    error: report.error,
    failures: report.failures,
    ...(hasOwn(result, 'metrics') && result.metrics !== undefined && { metrics: report.metrics }),
    truncated: report.truncated,
  });
}
