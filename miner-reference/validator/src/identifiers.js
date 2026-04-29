/**
 * Identifier rules from the Output Specification.
 *
 * - ALLOWED_ROOT_IDENTIFIERS: free references must come from this list (or be local bindings).
 * - FORBIDDEN_IDENTIFIERS: name-based, strict — any binding or reference matching these is rejected.
 * - COMPUTED_ACCESS_GATED: computed property access (`X['foo' + 'bar']`) is rejected on these objects.
 *
 * Edits to this file should be mirrored in output_specifications.md.
 */

export const ALLOWED_ROOT_IDENTIFIERS = new Set([
  'Math',
  'Number',
  'String',
  'Array',
  'Object',
  'Symbol',
  'JSON',
  'Map',
  'Set',
  'Boolean',
  'Error',
  'TypeError',
  'RangeError',
  'Infinity',
  'NaN',
  'undefined',
  'parseInt',
  'parseFloat',
  'isFinite',
  'isNaN',
  'BigInt',
  'ArrayBuffer',
  'DataView',
  'Int8Array',
  'Uint8Array',
  'Uint8ClampedArray',
  'Int16Array',
  'Uint16Array',
  'Int32Array',
  'Uint32Array',
  'Float32Array',
  'Float64Array',
  'BigInt64Array',
  'BigUint64Array',
]);

export const FORBIDDEN_IDENTIFIERS = new Set([
  'eval',
  'Function',
  'setTimeout',
  'setInterval',
  'setImmediate',
  'queueMicrotask',
  'fetch',
  'XMLHttpRequest',
  'WebSocket',
  'document',
  'window',
  'navigator',
  'localStorage',
  'sessionStorage',
  'indexedDB',
  'OffscreenCanvas',
  'HTMLCanvasElement',
  'crypto',
  'Date',
  'performance',
  'Proxy',
  'Reflect',
  'WeakRef',
  'FinalizationRegistry',
  'SharedArrayBuffer',
  'Atomics',
  'Worker',
  'process',
  'module',
  'global',
  'globalThis',
  'self',
  'require',
]);

export const COMPUTED_ACCESS_GATED = new Set([
  'THREE',
  'Math',
  'Object',
  'Array',
  'Symbol',
]);
