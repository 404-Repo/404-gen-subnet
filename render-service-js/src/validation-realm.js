/** Protect the shared validation realm before any submission executes. */
export function protectValidationRealm(THREE, codegenPrototypes, trappedGlobals) {
  let seed = 0x12345678;
  Math.random = () => {
    seed |= 0; seed = seed + 0x6D2B79F5 | 0;
    let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };

  for (const proto of codegenPrototypes) {
    Object.defineProperty(proto, 'constructor', {
      get() { throw new Error('Runtime violation: Function constructor is forbidden'); },
      configurable: false,
    });
  }

  // These constructors repeatedly assign the same prototype brand flag.
  // The setter permits that write without permitting the flag to change.
  for (const name of ['Vector2', 'Vector3', 'Vector4', 'Matrix2', 'Matrix3', 'Matrix4']) {
    Object.defineProperty(THREE[name].prototype, `is${name}`, {
      get() { return true; },
      set(value) {
        if (value !== true) throw new Error(`Runtime violation: ${name} brand is read-only`);
      },
      enumerable: true,
      configurable: false,
    });
  }

  const seen = new WeakSet();
  function harden(value, allowOwnOverrides = false) {
    if (value === null || (typeof value !== 'object' && typeof value !== 'function') || seen.has(value)) return;
    seen.add(value);
    for (const key of Reflect.ownKeys(value)) {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      harden(descriptor.value, allowOwnOverrides);
      // Instance callbacks may override inherited defaults; shared values cannot change.
      if (allowOwnOverrides && descriptor.configurable && descriptor.writable) {
        const original = descriptor.value;
        Object.defineProperty(value, key, {
          get() { return original; },
          set(next) {
            if (this === value) {
              if (next !== original) throw new Error('Runtime violation: shared Three.js state is read-only');
            } else {
              Object.defineProperty(this, key, {
                value: next, writable: true, enumerable: descriptor.enumerable, configurable: true,
              });
            }
          },
          enumerable: descriptor.enumerable,
          configurable: true,
        });
      }
      const protectedDescriptor = Object.getOwnPropertyDescriptor(value, key);
      harden(protectedDescriptor.get);
      harden(protectedDescriptor.set);
    }
    harden(Object.getPrototypeOf(value), allowOwnOverrides);
    Object.freeze(value);
  }

  const globals = [
    'Object', 'Function', 'Array', 'Number', 'String', 'Boolean', 'BigInt', 'Symbol',
    'Math', 'JSON', 'Date', 'RegExp', 'Promise', 'Map', 'Set', 'WeakMap', 'WeakSet',
    'WeakRef', 'FinalizationRegistry', 'Proxy', 'Reflect', 'Intl',
    'Error', 'EvalError', 'RangeError', 'ReferenceError', 'SyntaxError', 'TypeError', 'URIError', 'AggregateError',
    'ArrayBuffer', 'SharedArrayBuffer', 'DataView', 'Atomics',
    'Int8Array', 'Uint8Array', 'Uint8ClampedArray', 'Int16Array', 'Uint16Array',
    'Int32Array', 'Uint32Array', 'Float16Array', 'Float32Array', 'Float64Array',
    'BigInt64Array', 'BigUint64Array',
    'parseInt', 'parseFloat', 'isFinite', 'isNaN',
    'decodeURI', 'decodeURIComponent', 'encodeURI', 'encodeURIComponent',
  ];
  for (const name of globals) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
    if (!descriptor || !('value' in descriptor)) continue;
    harden(descriptor.value);
    if (!trappedGlobals.includes(name)) {
      Object.defineProperty(globalThis, name, { ...descriptor, writable: false, configurable: false });
    }
  }

  for (const proto of codegenPrototypes) harden(proto);
  for (const iterable of [[], '', new Map(), new Set()]) {
    harden(Object.getPrototypeOf(iterable[Symbol.iterator]()));
  }
  harden(Object.getPrototypeOf(''.matchAll(/x/g)));
  for (const name of Object.keys(THREE)) harden(THREE[name], true);

  return () => { seed = 0x12345678; };
}
