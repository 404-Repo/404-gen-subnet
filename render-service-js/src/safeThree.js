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
 * Two execution environments, one source:
 *
 *   - Node side (src/validation-worker.js). `createSafeThree(THREE)` reads
 *     the allowlists from `./validator/threeAllowlist.js` and builds a
 *     Proxy directly using the functions defined in this module.
 *
 *   - Browser side (src/render-pool.js, inside page.evaluate). Puppeteer's
 *     serialization means module imports do not cross the boundary, so we
 *     export `SAFE_THREE_BROWSER_BUNDLE` — a string containing the exact
 *     `Function.prototype.toString()` sources of the internal builders
 *     plus the public `buildSafeThreeWithConfig` factory. The renderer
 *     ships that string across the bridge and evaluates it inside the
 *     page BEFORE the page's `Proxy` / `Reflect` globals are trapped.
 *
 * Critically: the browser path does NOT maintain its own copy of the
 * Proxy construction logic. The bundle is materialized from the same
 * function references that `createSafeThree` calls, so the two execution
 * paths share one implementation and cannot drift. The runtime-adversary
 * suite runs every probe through BOTH paths to keep this honest.
 */

import {
  THREE_ALLOWED,
  THREE_BLOCKED_SUBMEMBERS,
  THREE_DISALLOWED_KNOWN,
} from './validator/threeAllowlist.js';

export function createSafeThree(THREE) {
  return buildSafeThreeWithConfig(THREE, {
    allowed: THREE_ALLOWED,
    disallowedKnown: THREE_DISALLOWED_KNOWN,
    blockedSubmembers: THREE_BLOCKED_SUBMEMBERS,
  });
}

/**
 * @param {object} THREE       The real THREE namespace (module or window.THREE).
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
        // TypeError rather than Error: semantically this is the same kind
        // of violation V8 itself raises when e.g. a proxy set trap returns
        // falsish, and downstream tests rely on a consistent error class
        // to distinguish "Proxy rejected" from unrelated crashes.
        throw new TypeError(`THREE.${parentName}.${prop} is forbidden`);
      }
      return target[prop];
    },
    has(target, prop) { return prop in target; },
    // Intentionally Object.* (not Reflect.*): `Reflect` is trapped inside
    // the browser-side render pipeline before miner code runs, so Proxy
    // traps that reference Reflect would throw at access time.
    ownKeys(target) { return Object.getOwnPropertyNames(target); },
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
      if (disallowedKnown.has(prop)) {
        // TypeError is the semantically correct class — see the sub-proxy
        // comment above for rationale.
        throw new TypeError(`${contextLabel}.${prop} is forbidden`);
      }
      throw new TypeError(`${contextLabel}.${prop} is not a recognized Three.js API`);
    },
    has(target, prop) {
      if (typeof prop !== 'string') return prop in target;
      return allowedKeys.has(prop);
    },
    ownKeys(target) { return Object.getOwnPropertyNames(target); },
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
// `SAFE_THREE_BROWSER_BUNDLE` is a self-contained JavaScript source string
// that, when evaluated, defines `buildSafeThreeWithConfig` and its
// supporting helpers. render-pool.js injects this string into Chromium via
// `page.evaluate` and invokes `buildSafeThreeWithConfig(window.THREE, cfg)`
// there — meaning the Node-side Proxy and the in-page Proxy are built from
// byte-identical function source.
//
// Any future change to the builder functions therefore propagates to both
// environments automatically. The runtime-adversary suite additionally
// reconstructs the proxy from this bundle and re-runs every probe through
// it, so any accidental mismatch (e.g. a helper defined but not included
// in the bundle list, or a closure capture introduced silently) fails
// noisily on first execution rather than much later in production.
//
// The function list ORDER matters only in that `buildSafeThreeWithConfig`
// is last — when a consumer of the bundle evaluates the string, the trailing
// expression is what they invoke. Hoisting handles intra-bundle references.
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
