/**
 * Runtime capability boundary for the THREE namespace.
 *
 * The static analyzer is a fast ergonomics layer — it gives miners clear
 * errors on well-formed code and rejects common bypass patterns before
 * execution. This module is the ACTUAL security boundary: no matter how
 * miner code routes THREE through aliases, destructuring, helpers,
 * containers, or future JS syntax we haven't seen, every access to a
 * `THREE.X` member *must* go through this Proxy's `get` trap, because the
 * JavaScript language makes no other path for property access to reach a
 * Proxied object.
 *
 * Design notes (the non-obvious bits):
 *
 *   1. The Proxy target is an ordinary (extensible, unfrozen) plain object
 *      populated only with allowlisted members — not the real THREE
 *      namespace. Module namespace objects are non-extensible with
 *      non-configurable exports, which makes Proxy invariants (especially
 *      `ownKeys` / `getOwnPropertyDescriptor`) reject any attempt to hide
 *      or add names. Proxying a plain backing object avoids the entire
 *      class of invariant problems.
 *
 *   2. `get` returns the real class for allowlisted names, a nested Proxy
 *      for members with blocked submembers (e.g. `MathUtils.seededRandom`),
 *      and throws a descriptive `TypeError` for forbidden / unknown names.
 *
 *   3. All mutation traps (`set`, `deleteProperty`, `defineProperty`,
 *      `setPrototypeOf`, `preventExtensions`) return `false`. Under strict
 *      mode (which miner code runs in), this turns mutation attempts into
 *      `TypeError`s rather than silent no-ops.
 *
 *   4. `getPrototypeOf` returns `Object.prototype` rather than the real
 *      namespace's prototype so the prototype chain doesn't leak anything.
 *
 *   5. `SAFE_THREE_BROWSER_BUNDLE` exports a string of function sources so
 *      a consumer in a different realm (e.g. a Puppeteer page, a Web Worker,
 *      or a future browser-side validator) can rebuild the exact same Proxy
 *      without reimplementing the logic. Production miner-reference has no
 *      browser path today, but the bundle is exported for parity with
 *      render-service-js — the two safeThree modules should stay in lock
 *      step, and the runtime-adversary test here exercises both entry
 *      points so divergence shows up immediately.
 */

import {
  THREE_ALLOWED,
  THREE_BLOCKED_SUBMEMBERS,
  THREE_DISALLOWED_KNOWN,
} from './threeAllowlist.js';

export function createSafeThree(THREE) {
  return buildSafeThreeWithConfig(THREE, {
    allowed: THREE_ALLOWED,
    disallowedKnown: THREE_DISALLOWED_KNOWN,
    blockedSubmembers: THREE_BLOCKED_SUBMEMBERS,
  });
}

/**
 * @param {object} THREE       The real THREE namespace.
 * @param {object} config
 *   allowed:           Set<string> | string[]
 *   disallowedKnown:   Set<string> | string[]
 *   blockedSubmembers: Record<string, Set<string> | string[]>
 */
export function buildSafeThreeWithConfig(THREE, config) {
  const allowed = toSet(config.allowed);
  const disallowedKnown = toSet(config.disallowedKnown);
  const blockedSubmembers = toBlockedMap(config.blockedSubmembers);

  const backing = buildAllowedBackingObject(THREE, allowed, blockedSubmembers);
  return wrapWithProxy(backing, {
    contextLabel: 'THREE',
    disallowedKnown,
    allowedKeys: allowed,
  });
}

// ── internals ────────────────────────────────────────────────────────────────

function toSet(v) {
  if (v instanceof Set) return v;
  return new Set(v || []);
}

function toBlockedMap(v) {
  const out = Object.create(null);
  if (!v) return out;
  for (const key of Object.keys(v)) {
    out[key] = toSet(v[key]);
  }
  return out;
}

function buildAllowedBackingObject(THREE, allowed, blockedSubmembers) {
  const backing = {};
  for (const name of allowed) {
    if (!(name in THREE)) continue;
    const blocked = blockedSubmembers[name];
    backing[name] = blocked && blocked.size
      ? makeSafeSubmember(name, THREE[name], blocked)
      : THREE[name];
  }
  return backing;
}

function makeSafeSubmember(parentName, realObj, blocked) {
  const filtered = {};
  if (realObj && typeof realObj === 'object') {
    for (const k of Object.keys(realObj)) {
      if (!blocked.has(k)) filtered[k] = realObj[k];
    }
  }
  return new Proxy(filtered, {
    get(target, prop) {
      if (typeof prop !== 'string') return target[prop];
      if (blocked.has(prop)) {
        // TypeError rather than Error: this is semantically the same kind
        // of violation V8 raises for mutation-trap rejections, and the
        // runtime adversary suite pins the error class to distinguish
        // "Proxy rejected" from unrelated crashes.
        throw new TypeError(`THREE.${parentName}.${prop} is forbidden`);
      }
      return target[prop];
    },
    has(target, prop) {
      return prop in target;
    },
    // NOTE: deliberately using Object.* instead of Reflect.* in trap bodies.
    // The browser-side render pipeline traps `Reflect` globally before miner
    // code executes; traps that rely on Reflect would throw at access time.
    // `Object.getOwnPropertyNames` / `Object.getOwnPropertyDescriptor` cover
    // everything we need on our plain-object backing (no symbols present).
    ownKeys(target) {
      return Object.getOwnPropertyNames(target);
    },
    getOwnPropertyDescriptor(target, prop) {
      return Object.getOwnPropertyDescriptor(target, prop);
    },
    set() { return false; },
    deleteProperty() { return false; },
    defineProperty() { return false; },
    setPrototypeOf() { return false; },
    preventExtensions() { return false; },
    getPrototypeOf() { return Object.prototype; },
  });
}

function wrapWithProxy(backing, { contextLabel, disallowedKnown, allowedKeys }) {
  return new Proxy(backing, {
    get(target, prop) {
      if (typeof prop !== 'string') return target[prop];
      if (prop in target) return target[prop];
      // Name is not in the allowlist — throw with a descriptive message
      // so miner debugging is pleasant, but never return a value. We throw
      // TypeError rather than plain Error for the reason described on the
      // sub-proxy's get trap above.
      if (disallowedKnown.has(prop)) {
        throw new TypeError(`${contextLabel}.${prop} is forbidden`);
      }
      throw new TypeError(`${contextLabel}.${prop} is not a recognized Three.js API`);
    },
    has(target, prop) {
      if (typeof prop !== 'string') return prop in target;
      return allowedKeys.has(prop);
    },
    ownKeys(target) {
      return Object.getOwnPropertyNames(target);
    },
    getOwnPropertyDescriptor(target, prop) {
      if (typeof prop === 'string' && !allowedKeys.has(prop)) return undefined;
      return Object.getOwnPropertyDescriptor(target, prop);
    },
    set() { return false; },
    deleteProperty() { return false; },
    defineProperty() { return false; },
    setPrototypeOf() { return false; },
    preventExtensions() { return false; },
    getPrototypeOf() { return Object.prototype; },
  });
}

// ── browser bundle (single source of truth) ─────────────────────────────────
//
// A self-contained JS source string that defines `buildSafeThreeWithConfig`
// and its helpers when evaluated in any realm. Miner-reference has no
// browser path today, but this export exists for parity with
// render-service-js/src/safeThree.js (the two modules are kept in sync),
// and the runtime-adversary test here exercises both the direct
// `createSafeThree` factory and a proxy rebuilt from this bundle so the
// two entry points cannot diverge.
const BUILDERS_FOR_BUNDLE = [
  toSet,
  toBlockedMap,
  makeSafeSubmember,
  buildAllowedBackingObject,
  wrapWithProxy,
  buildSafeThreeWithConfig,
];

export const SAFE_THREE_BROWSER_BUNDLE =
  BUILDERS_FOR_BUNDLE.map((fn) => fn.toString()).join('\n\n');
