# @404-subnet/validator

Static and post-execution validator for 404-GEN miner submissions.

This package mirrors the production runtime's static analysis and post-execution checks. Miners can run it locally to iterate on their `generate.js` files before submitting.

## What it checks

| Stage | Rules |
|---|---|
| parse | `FILE_SIZE_EXCEEDED`, `PARSE_ERROR` |
| static_analysis | `MISSING_DEFAULT_EXPORT`, `MULTIPLE_TOP_LEVEL_EXPORTS`, `ASYNC_NOT_ALLOWED`, `FORBIDDEN_IDENTIFIER`, `IDENTIFIER_NOT_ALLOWED`, `FORBIDDEN_THREE_API`, `UNKNOWN_THREE_API`, `THREE_AT_TOP_LEVEL`, `THREE_ALIAS_FORBIDDEN`, `COMPUTED_PROPERTY_ACCESS`, `LITERAL_BUDGET_EXCEEDED` |
| module_load / execution | `EXECUTION_THREW`, `INVALID_RETURN_TYPE`, `TIMEOUT_EXCEEDED`, `ASYNC_NOT_ALLOWED` |
| post_validation | `INVALID_RETURN_TYPE`, `EMPTY_SCENE`, `CYCLE_DETECTED`, `VERTEX_LIMIT_EXCEEDED`, `DRAW_CALL_LIMIT_EXCEEDED`, `DEPTH_LIMIT_EXCEEDED`, `INSTANCE_LIMIT_EXCEEDED`, `TEXTURE_DATA_EXCEEDED`, `BOUNDING_BOX_OUT_OF_RANGE` |

Rule codes match the `Rule Code` column in `output_specifications.md` § Failure Semantics.

## Execution model

Miner code runs inside a **Node.js worker thread** spawned by `src/execute.js`. The worker loads the miner module, calls `generate(safeTHREE)` synchronously (note: **not** the raw `THREE` namespace — see the `safeTHREE` section below), and runs post-validation. The main thread only sees the JSON summary. This gives us three real guarantees that the previous single-process implementation could not provide:

1. **Preemptive timeout.** `worker.terminate()` from the main thread kills the worker instantly, even in the middle of a synchronous CPU-bound loop. Infinite loops in either module top-level code or `generate()` itself are cut off at exactly the 5-second budget. Matches the behavior of production `isolated-vm`.
2. **Module load counted against the budget.** The wall-clock timer starts *before* the worker is constructed, so top-level work in the miner's module (constant initializers, class declarations, eager computation) eats the 5-second budget. Matches Runtime Spec § Combined Budget.
3. **V8 heap cap enforced.** The worker is spawned with `resourceLimits: { maxOldGenerationSizeMb: 256 }`. A miner allocating 10M objects trips `ERR_WORKER_OUT_OF_MEMORY`, and the main thread reports `HEAP_EXCEEDED`. Note: the cap applies to the **V8 JavaScript heap only**, not external `ArrayBuffer` / typed-array memory — same as production `isolated-vm`'s `memoryLimit`. A miner shipping a 400 MB `Float32Array` is caught by `VERTEX_LIMIT_EXCEEDED` long before memory becomes relevant.

## What it does NOT check

This package is a **conformance tool**, not a security sandbox.

- **No V8 isolate boundary.** The worker shares some built-ins with the host process (e.g. `console`). Static analysis rejects forbidden identifiers, so this is an isolation gap, not a correctness gap — a well-formed submission that passes here also passes production. A malicious miner can do whatever they want on their own machine; the tool only tells them whether their *honest* code conforms.
- **No render run.** The conformance tool does not run Puppeteer. Production uses a double-execute strategy (validate in `isolated-vm`, render in a Puppeteer page); the local tool only runs the validation half. If your code passes here but fails to *render* in production (extremely unlikely given determinism), you'll see a `RENDER_RUN_FAILED` only in production logs.
- **No external `ArrayBuffer` memory cap.** The V8 heap cap doesn't cover typed-array backing buffers. Bounded instead by the vertex-count and literal-budget caps.

The static analysis stage is bit-identical to the production validator's. The execution and post-validation stages match the production *rules* exactly, with the isolation gap noted above.

## safeTHREE — the runtime capability boundary

`src/safeThree.js` wraps the real Three.js namespace in a `Proxy` that returns only allowlisted members and throws on everything else. This is the canonical enforcement layer for *which Three.js APIs miner code is allowed to touch* — the static analysis above is the same logic expressed at AST level, giving fast and precise error messages on well-formed code, but the Proxy is what provides the actual guarantee.

Why both layers exist:

- **Static analysis** gives the miner a clear error on line `N` before anything runs. It is the ergonomics layer. It has been shown — twice, in production review — to miss syntactic corners (`this.t = THREE`, `obj.t = THREE`). Those cases were fixed, but the pattern is general: static analysis over a language as flexible as JavaScript will always have corners.
- **safeTHREE Proxy** makes the failure mode *unconditional*: no matter how miner code launders its `THREE` binding — aliases, destructures, helper forwarding, container stashing, reflective enumeration, any future JS syntax — every access to a `THREE.X` member must flow through the Proxy's `get` trap and is either allowed or thrown there. This is not a convention; it is the JavaScript property-access model.

The proxy also:

- Filters `THREE.MathUtils` through a sub-Proxy that blocks `seededRandom` and `generateUUID` (non-deterministic helpers).
- Rejects `set` / `delete` / `defineProperty` on the namespace (strict mode turns these into TypeErrors).
- Hides forbidden names from `Object.getOwnPropertyNames` and `in` (no leak through reflection).

Covered by `test/run-runtime-adversary.mjs`, which re-runs every known attack pattern against the runtime and asserts each path either throws or is trivially harmless. The discipline is: any new static-analysis rule should have a matching runtime probe in that file, so the two layers are verified to agree.

## Install

```bash
cd validator
npm install
```

## Use as a library

```js
import { validate } from '@404-subnet/validator';
import fs from 'node:fs/promises';

const source = await fs.readFile('./my-generate.js', 'utf8');
const result = await validate(source);

if (result.passed) {
  console.log('OK', result.metrics);
} else {
  console.log('FAILED', result.failures);
}
```

## Use via the CLI

See `../tools/validate.js` for the CLI wrapper.

```bash
node ../tools/validate.js ../examples/car.js
```

## Result shape

```ts
{
  passed: boolean,
  stagesRun: string[],
  failures: Array<{ stage: string, rule: string, detail: string }>,
  metrics: {
    vertices: number,
    drawCalls: number,
    maxDepth: number,
    instances: number,
    textureBytes: number,
    bbox: { min: { x, y, z }, max: { x, y, z } } | null,
  } | null,
  moduleLoadMs: number | null,  // time spent importing the module (worker-measured)
  executionMs: number | null,   // time spent inside generate() (worker-measured)
  totalMs: number,              // total wall-clock time (main-thread measured), always present
}
```

`totalMs` is the total time from the moment the worker is spawned to the moment it posts its result (or is terminated by the timeout). `moduleLoadMs + executionMs` is the worker's accounting of the same budget and is always ≤ `totalMs`. The difference is worker startup and Three.js import overhead, typically 50–150 ms.

## Spec source of truth

The two specifications this validator implements:

- `../output_specifications.md` — what miners must produce
- `../runtime_specifications.md` — how the production validator enforces the rules

When the spec changes, update `src/identifiers.js` and `src/threeAllowlist.js` to match. In a production build, both files should be generated mechanically from the markdown so they cannot drift.
