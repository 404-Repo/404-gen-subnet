# Three.js Runtime Specification

> **Note:** This document describes how the validator loads, sandboxes, executes, and renders miner submissions. It is the companion to the **Three.js Output Specification**. Both documents are required reading.

## Overview

The runtime has three responsibilities:

1. **Validate** the submitted JavaScript module statically before any code runs.
2. **Execute** the `generate` function in an isolated sandbox and collect the returned Three.js scene root.
3. **Render** the scene under a fixed, canonical camera and lighting setup for judging.

Every stage is defensive. Miners are untrusted. Assume any submission is hostile.

## Three.js Bundle

- **Version:** `three@0.183.2` (r183)
- **Source:** `three/build/three.module.js` from the npm registry, loaded as source text inside the isolate (see Sandbox Execution).
- **Integrity hash:** [TBD — computed at pipeline setup, pinned per competition round]

The bundle is loaded **inside** the sandbox isolate via `isolate.compileModule()`, not transferred from the host. This keeps property access at native V8 speed instead of going through expensive IPC boundaries per access.

The exact bundle is frozen for the duration of a competition round. Between rounds, the version may be updated; miners are notified in advance.

## Module Loading

Miners submit a single ES module file. The runtime loads it as follows:

1. Read the source as UTF-8 text.
2. Reject if file size exceeds 1 MB.
3. Parse with `@babel/parser` in `sourceType: "module"` mode. Reject on syntax errors.
4. Verify exactly one top-level export: `export default` of a function declaration or expression.
5. Run static analysis (see next section).
6. If static analysis passes, compile the source inside the sandbox isolate as an ES module alongside the pre-loaded Three.js module.

The miner's module is never loaded into the validator's main process. It only exists inside the sandbox.

## Static Analysis

Performed on the AST before execution. Any violation causes immediate rejection.

### Async and Top-Level Await Ban

- If the default export is an `AsyncFunctionDeclaration` or `AsyncFunctionExpression`: rejection.
- If the module body contains any `AwaitExpression` not enclosed in an async function: rejection.
- If the module body contains `for await`: rejection.
- If the module contains `AsyncGeneratorDeclaration` or `AsyncGeneratorExpression`: rejection.

### Allowed Root Identifiers

The analyzer walks the AST and checks every `Identifier` referenced at read position. Only the following root identifiers are allowed:

`Math`, `Number`, `String`, `Array`, `Object`, `Symbol`, `JSON`, `Map`, `Set`, `Boolean`, `Error`, `TypeError`, `RangeError`, `Infinity`, `NaN`, `undefined`, `parseInt`, `parseFloat`, `isFinite`, `isNaN`, `BigInt`, `ArrayBuffer`, `DataView`, `Int8Array`, `Uint8Array`, `Uint8ClampedArray`, `Int16Array`, `Uint16Array`, `Int32Array`, `Uint32Array`, `Float32Array`, `Float64Array`, `BigInt64Array`, `BigUint64Array`.

Plus local bindings declared within the miner's module (function parameters, `const`/`let`/`var` declarations).

`THREE` is **not** in this list as a root identifier. It is only available as the parameter of the default export function — top-level code cannot reference it. Any top-level reference to `THREE` outside the `generate` function body causes rejection.

Any identifier outside this list is rejected.

### Blocked Properties on Allowed Identifiers

Separate from the identifier allowlist, specific `MemberExpression` patterns are rejected even when the root identifier is allowed:

- `Math.random`
- `THREE.MathUtils.seededRandom`
- `THREE.MathUtils.generateUUID`

### Forbidden Identifiers

These identifiers are explicitly rejected if referenced anywhere:

`eval`, `Function`, `setTimeout`, `setInterval`, `setImmediate`, `queueMicrotask`, `fetch`, `XMLHttpRequest`, `WebSocket`, `document`, `window`, `navigator`, `localStorage`, `sessionStorage`, `indexedDB`, `OffscreenCanvas`, `HTMLCanvasElement`, `crypto`, `Date`, `performance`, `Proxy`, `Reflect`, `WeakRef`, `FinalizationRegistry`, `SharedArrayBuffer`, `Atomics`, `Worker`, `process`, `module`, `global`, `globalThis`, `self`, `require`, `import`.

`Date` is forbidden in all forms — `Date.now()`, `new Date()`, and bare `Date` references.

**Scoping rule (strict, name-based).** Forbidden identifier checks are name-based and not scope-aware. Any binding or reference matching a forbidden name is rejected, even when the binding is local. Miners cannot name a local variable `fetch`, `document`, `window`, etc. — rename to something else.

**Property name exemption.** Forbidden identifier checks apply only to `Identifier` nodes in expression position (read or write of a binding). Property names in `MemberExpression` (`obj.fetch`) and key names in `ObjectExpression` (`{ fetch: false }`) are **not** checked. Object keys and property accesses on non-restricted objects may use any name.

**Allowed constructs.** Classes (`ClassDeclaration`, `ClassExpression`, class fields, methods) are allowed. Synchronous generator functions (`function*`) are allowed. Destructuring patterns (object/array destructuring in declarations and parameters) are recognized as local binding declarations and walked accordingly.

### Computed Property Access Ban

The analyzer rejects any `MemberExpression` with `computed: true` on `THREE`, `Math`, `Object`, `Array`, `Symbol`, and all Three.js identifiers. Only direct property access (`THREE.BoxGeometry`) is allowed.

### THREE Member Allowlist

Every `MemberExpression` whose object is the `THREE` parameter is checked against an explicit allowlist of permitted Three.js identifiers. The allowlist is the union of every identifier listed under **Output Specification § Allowed Three.js APIs** — geometry classes, materials, textures, math classes, curves, objects, and constants.

Any `THREE.X` access where `X` is not on the allowlist is rejected with rule code `FORBIDDEN_THREE_API`. Examples:

- `new THREE.BoxGeometry(2, 1, 4)` — allowed (`BoxGeometry` is on the list).
- `THREE.SRGBColorSpace` — allowed (constant on the list).
- `new THREE.AnimationMixer(scene)` — rejected (`AnimationMixer` is not on the list and is explicitly prohibited in the Output Spec).
- `new THREE.SkinnedMesh(geo, mat)` — rejected.
- `new THREE.MeshLambertMaterial({})` — rejected (non-PBR materials are forbidden).
- `THREE.GLTFLoader` — rejected (loaders are not on the list, and additionally are not exported by the bare `three` module).

This is a default-deny rule. New Three.js APIs added in future versions are automatically rejected until the allowlist is updated for that round.

The allowlist is generated mechanically from the Output Spec at pipeline build time and pinned alongside the Three.js bundle integrity hash. It cannot drift between the spec and the validator.

### Literal Byte Budget

The analyzer walks all literal nodes and sums byte sizes per the Output Spec:

- `StringLiteral`: UTF-8 byte length of the raw value.
- `TemplateLiteral` quasis: UTF-8 byte length of each quasi (interpolation expressions walked separately).
- `NumericLiteral`: byte length of the source text representation.
- `BigIntLiteral`: byte length of the source text representation.

If the sum exceeds 50 KB, rejection.

## Sandbox Execution

### Isolation Layer

Miner code runs inside a Node.js `isolated-vm` context. This provides:

- A separate V8 isolate with no shared memory or globals with the host process.
- No access to Node.js built-ins (`fs`, `child_process`, `http`, etc.).
- No access to the host filesystem or network.
- Hard memory limit enforced at the V8 level: **256 MB**.
- Execution timeout enforced at the V8 level: **5 seconds** wall-clock.

The isolate is created fresh for each submission and destroyed immediately after. State never leaks between runs.

### THREE as Argument Only

Three.js is loaded inside the isolate as an ES module at setup time (see below), but the `THREE` namespace object is **not** placed on the global scope. It is passed explicitly as the argument to `generate` when the validator invokes the default export.

Top-level code in the miner's module cannot reference `THREE`. This is enforced by both static analysis (identifier allowlist) and runtime scope (no global binding). As a consequence:

- No geometry, textures, or materials can be constructed at module load time.
- All asset construction happens inside `generate`, bounded by the execution timeout.
- Helper functions that need `THREE` must receive it as a parameter.

### Setup Sequence

Per submission:

1. Create a fresh `ivm.Isolate` with `memoryLimit: 256`.
2. Create a context with no default globals beyond the allowed root identifiers listed above.
3. Compile the Three.js source as an ES module: `isolate.compileModule(threeSource)`.
4. Instantiate and evaluate the Three.js module. Retrieve its namespace as a reference inside the isolate.
5. **Freeze the THREE namespace** via `Object.freeze` on the top-level namespace object. This prevents miners from swapping out class references between construction and post-execution validation. Deep-freezing nested objects is avoided because Three.js mutates some internal class state at runtime; only the top-level namespace is frozen. This top-level-only freeze relies on determinism for full safety: any mutation a miner makes to nested Three.js prototypes happens identically in both the validation and render runs (the same source executes twice), so the metrics measured in run 1 always match the scene rendered in run 2.
6. Replace non-deterministic and forbidden properties on built-ins using property-descriptor replacement:
   - `Object.defineProperty(Math, 'random', { get() { throw new Error('Math.random is forbidden'); }, configurable: true })`
   - Same pattern for any other non-configurable properties that cannot be deleted.
7. Compile the miner's source as an ES module: `isolate.compileModule(minerSource)`.
8. Instantiate the miner module with a link callback that resolves no external specifiers (miners cannot import anything).
9. Evaluate the miner module. **Module evaluation counts against the 5-second timeout and 256 MB heap cap.**
10. Extract the default export from the miner module's namespace.
11. Invoke it with `THREE` as the argument: `generateFn.apply(undefined, [threeNamespace], { timeout: remainingBudget })`, where `remainingBudget = 5000ms − (time elapsed during steps 7–10)`. The host computes this delta before invocation.
12. Capture the return value as a reference.

### Combined Budget

The 5-second wall-clock limit and 256 MB heap limit apply to the **combined** duration and memory of module evaluation plus the `generate()` invocation. Module-level computation counts against both budgets. Miners cannot offload work to the top level to bypass the limit.

### Host Process Isolation

The isolated-vm context runs inside a disposable Docker container:

- `--network=none` — no network access at all.
- `--read-only` root filesystem; `/tmp` is a small tmpfs scratch space.
- Non-root user, all Linux capabilities dropped.
- Seccomp profile restricting syscalls.
- `--cpus=2 --memory=4g` resource limits.
- `--pids-limit=256` — fork bomb defense.
- `--ulimit nofile=1024`.
- No host filesystem bind mounts, no Docker socket access.

Each container processes exactly one submission and is destroyed afterward.

## Post-Execution Validation

After `generate` returns successfully, the runtime performs the checks defined in the Output Spec.

### Return Type

Must be `Group`, `Mesh`, `LineSegments`, or `Points`. Single-mesh returns are wrapped in a new `Group` automatically.

### Bounded Traversal with Cycle Detection

Three.js's `Object3D.traverse()` does not detect cycles. The validator uses an explicit bounded walk:

```
const visited = new Set();
function walk(node, depth) {
  if (visited.has(node)) throw new CycleError();
  if (depth > MAX_DEPTH) throw new DepthError();
  visited.add(node);
  for (const child of node.children) walk(child, depth + 1);
}
```

This walk produces all the metrics below in a single pass.

### Metrics Computed During the Walk

1. **Bounding box.** `new THREE.Box3().setFromObject(root)`. Rejected if any axis is outside `[-0.5, 0.5]` or if the box is empty.
2. **Vertex count.** Sum of `geometry.attributes.position.count` across all reachable meshes. `InstancedMesh` counted once (not multiplied by instances). Limit: **250,000**.
3. **Draw call count.** Nodes where `isMesh`, `isLine`, or `isPoints` is true. `InstancedMesh` counted as 1. Limit: **200**.
4. **Scene-graph depth.** Maximum depth reached during the walk. Limit: **32**.
5. **Instance count.** Sum of `instance.count` read directly from each `InstancedMesh` at validation time (not from constructor arguments — miners can reassign `count` post-construction). Limit: **50,000**.
6. **Texture data.** Sum of byte lengths of all `DataTexture.image.data` buffers reachable via material references. Limit: **1 MB (1,048,576 bytes)**.

Any metric exceeding its limit causes failure.

## Rendering

### Strategy: Double-Execute

Validated submissions are rendered in a second execution of the same source code, not by serializing the scene from the sandbox. The workflow:

1. **Validation run.** The miner's source executes inside the `isolated-vm` sandbox. All post-execution checks run. The returned root and the sandbox are discarded.
2. **Render run.** If validation passed, the same source is executed a second time inside a headless Chrome page via Puppeteer, with Three.js loaded in the page context. The returned root is added to the canonical scene and rendered.

This is safe because the determinism rules in the Output Spec guarantee both runs produce **identical** scenes. The metrics measured in run 1 match the scene rendered in run 2 exactly. The trust boundary stays in `isolated-vm`; Puppeteer is only a display device.

### Rationale

Serializing a Three.js scene across contexts is fragile: `Object3D.toJSON()` is lossy for several material and texture types, color spaces can drift on round-trip, and `DataTexture` buffers and custom `BufferAttribute`s may not survive. Double-execution avoids the entire serialization surface. The cost is one extra construction pass, which is bounded by the 5-second budget and typically runs in milliseconds.

Double-execution is safe because the determinism rules in **Output Spec § Determinism** (no `Math.random`, no `Date`, no environment access, no stateful Three.js helpers) guarantee bit-identical output across runs. Both the validation run and the render run execute the same source over the same frozen `THREE` namespace and construct exactly the same scene. The metrics measured in run 1 always describe the asset rendered in run 2.

### Render Run Sandbox

The Puppeteer page enforces the same constraints as the `isolated-vm` sandbox:

- A localhost HTTP server inside the container serves the render host HTML and the pinned Three.js bundle. `--network=none` blocks the external network namespace, not loopback, so localhost serving works without exposing anything outside the container.
- The render host HTML is loaded with a tight Content-Security-Policy that blocks all external script and resource loading, allowing only `'self'` and `blob:` sources.
- Three.js is loaded into the page from the pinned local bundle (same integrity hash as the validation run).
- The miner's source is injected as an ES module via a **blob URL**: `URL.createObjectURL(new Blob([source], { type: 'application/javascript' }))`, then loaded as `<script type="module" src="blob:...">`. This avoids inline scripts and works under strict CSP.
- **Property override ordering.** The override script that replaces `Math.random`, `Date`, and other forbidden properties on `window` is injected as a synchronous (non-module) `<script>` tag in the render host HTML, placed **before** the miner's `<script type="module">` tag. Browser ordering guarantees the synchronous script runs to completion before any deferred module script begins evaluation.
- The validator's host page calls the miner's default export with the page's local `THREE` reference once the module loads.
- Network access is blocked at the container level (`--network=none`) for everything outside loopback, and additionally via `page.setOfflineMode(true)`.

**Render run timeout.** The render run has its own 5-second budget covering **page load + miner module load + miner module evaluation + the call to `generate()`**, mirroring the validation run's combined budget. The canonical render pass that follows (lighting, draw calls, framebuffer readback) is host code, not miner code, and is **not** counted against this budget — it has its own separate timeout.

**Heap enforcement.** Puppeteer does not have a per-page heap cap analogous to `isolated-vm.memoryLimit`. The render run relies on two protections instead: (1) the validation run already proved the same source constructs within 256 MB, and determinism guarantees the render run will too; (2) the container `--memory=4g` cap will OOM-kill anything pathological. Chromium is launched with `--js-flags="--max_old_space_size=512"` to leave headroom for Puppeteer's own JS while still bounding the renderer process.

The render host HTML page is content-addressed and version-pinned per round, the same way Three.js is. Its integrity hash is recorded alongside the Three.js hash and listed in the round-pinning manifest.

If the render run fails or exceeds budget, the prompt is marked failed even if validation passed. This should be exceptionally rare given determinism.

### Renderer

- `THREE.WebGLRenderer` with `{ antialias: true, preserveDrawingBuffer: true }`
- Output canvas size: **1024 × 1024**
- Tone mapping: `THREE.ACESFilmicToneMapping`
- Tone mapping exposure: `1.0`
- Output color space: `THREE.SRGBColorSpace`
- Pixel ratio: `1`

### Camera

- `THREE.PerspectiveCamera`
- FOV: **17.5 degrees** (telephoto-ish, minimizes perspective distortion to match typical product photography in prompt images)
- Aspect: `1.0`
- Near: `0.01`, Far: `100`
- Position: `(0, 0.3, 3.5)`
- Target: `(0, 0, 0)`
- Up: `(0, 1, 0)` (Y-up)

The camera looks down -Z. Models facing +Z are front-facing. At FOV 17.5° and distance 3.5, the camera frames a region of approximately 1.08 units across the focal plane, which tightly fits an asset filling the `[-0.5, 0.5]` bounding cube.

### Lighting

- **Ambient light:** `AmbientLight(0xffffff, 0.15)`
- **Key light:** `DirectionalLight(0xffffff, 1.2)` at `(2, 3, 2)`, target `(0, 0, 0)`
- **Fill light:** `DirectionalLight(0xffffff, 0.4)` at `(-2, 1, 1)`, target `(0, 0, 0)`
- **Environment map:** neutral studio HDRI (exact file and integrity hash: [TBD])
- **Environment intensity:** `1.0`

### Background

Solid neutral gray (`0x808080`).

### Render Pass

1. Build the canonical scene with lighting and environment.
2. Add the miner's root (from the render run) to the scene.
3. Render to the offscreen framebuffer.
4. Extract as PNG.
5. Pass to the judging pipeline.

No post-processing.

## Error Reporting

For each failed prompt, miners receive a structured failure reason:

```json
{
  "stage": "static_analysis" | "module_load" | "execution" | "post_validation" | "render",
  "rule": "FORBIDDEN_IDENTIFIER",
  "detail": "fetch"
}
```

- `stage` tells the miner which phase failed:
  - `static_analysis` — AST parsing or static rule violation, before any code runs.
  - `module_load` — compiling and evaluating the miner's module body, before `generate()` is called. Errors here include instantiation failures or top-level code throwing.
  - `execution` — the call to `generate(THREE)` itself.
  - `post_validation` — checks on the returned root (vertex count, bounding box, etc.).
  - `render` — the second-execution render run failed.
- `rule` is a stable machine-readable code defined in the **Output Spec § Failure Semantics** table (the `Rule Code` column lists every code the runtime can emit). Codes are stable across rounds; new codes are added only when new constraints are introduced and are announced in advance.
- `detail` provides minimal context (which identifier, which limit, which axis) — never a full stack trace and never internal paths.

Stack traces from inside the sandbox are not returned, as they may leak runtime internals.

## Resource Accounting

Each submission logs for analysis and abuse detection:

- Parse time, static analysis time, sandbox setup time
- Module evaluation time
- `generate` execution wall-clock time
- Peak JS heap usage **and the stage at which peak was reached** (module load / generate / post-validation walk)
- Returned scene metrics (vertex count, draw calls, depth, instances, texture bytes)
- Render run construction time (time the miner's source spent rebuilding the scene in Puppeteer)
- Canonical render pass time (lighting setup, draw calls, framebuffer readback)

These metrics are not used for scoring directly, but systematic outliers may trigger manual review.

## Changes Between Rounds

The following may change between competition rounds and will be announced in advance:

- Three.js bundle version and integrity hash
- Render host HTML page and integrity hash
- Canvas resolution
- Camera, lighting, and environment configuration
- Resource limits
- Allowed Three.js API surface

Within a single round, all values are frozen at round start.

## See Also

- **Three.js Output Specification** — what miners must produce.
- **API Specification** — `/generate`, `/status`, `/results` endpoints.
- **Competition Rules** — scoring, prompt sets, time budgets, pod management.