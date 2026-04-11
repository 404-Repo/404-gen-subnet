# Three.js Output Specification

> **Note:** This document specifies what miners must produce. A separate **Runtime Specification** describes how submissions are loaded, sandboxed, executed, and rendered by the validator. Both documents are required reading.

[↓ TL;DR](#tldr)

## Concept

For every input prompt, your solution emits one JavaScript module that constructs the corresponding 3D asset from scratch using Three.js primitives. Each module is self-contained: it has no inputs, no dependencies, and no access to the prompt at runtime — your model has already encoded the prompt as code.

The validator does not pass any prompt-related context to your function. By construction, your code cannot read the prompt at runtime — this is enforced impossibility, not convention.

## Function Signature

Your solution must produce a JavaScript module that exports a default function:

```js
export default function generate(THREE) {
  const group = new THREE.Group();

  // Your procedural geometry here

  return group;
}
```

The function receives a `THREE` instance as its only argument (see Runtime Spec for the exact bundle).

Your module must not import or require any external package. The `THREE` object passed to `generate` is the only API surface available. Any `import` or `require` statement causes rejection at parse time. The module must have exactly one top-level export: the default `generate` function.

The function must be synchronous. `async function` declarations and any return value that is a `Promise` are treated as failed prompts.

**`THREE` is bound only as the parameter of `generate`.** It is not available at module top level. Any top-level reference to `THREE` is rejected at parse time. No geometry, textures, or materials can be constructed at module load — all asset construction must happen inside `generate`. Helper functions that need `THREE` must accept it as a parameter.

## Return Value

The function must return a `THREE.Group`, `THREE.Mesh`, `THREE.LineSegments`, or `THREE.Points`. Returning `null`, `undefined`, throwing, or returning any other type is treated as a failed prompt. The returned root must satisfy all geometry, scale, and validation constraints — partial returns are not graded leniently.

Single `Mesh`, `LineSegments`, or `Points` returns are automatically wrapped in a `Group` by the validator. You don't need to wrap them yourself, but doing so is harmless.

## What You Control

- Geometry (vertices, faces, normals, UVs)
- Materials (`MeshStandardMaterial`, `MeshPhysicalMaterial`, `MeshBasicMaterial`, `PointsMaterial`, `LineBasicMaterial`, `LineDashedMaterial`)
- Procedurally generated textures via `DataTexture` only
- Vertex colors
- Object hierarchy within the returned root

If you use a `DataTexture` on a material, your `BufferGeometry` must define a `uv` attribute. Primitive geometries provide UVs automatically; custom `BufferGeometry` does not.

Custom vertex attributes beyond `position`, `normal`, `uv`, and `color` are allowed but not used by the validator's rendering pipeline.

## What You Don't Control

- Camera position, angle, FOV, aspect ratio
- Lighting setup
- Renderer settings
- Canvas resolution
- Background / environment
- Post-processing

## Coordinate System & Scale

- The asset must fit within a unit bounding box of `[-0.5, 0.5]` on all axes.
- Origin at the geometric center of the asset.
- Y-up orientation.
- **Forward axis: +Z.** The camera looks down -Z, so models should face +Z. This is the Three.js default convention.
- The validator will not rescale or reposition your asset. Out-of-bounds assets are rejected.

## Rendering Context

Your asset is rendered under a fixed camera, lighting, and environment setup defined in the Runtime Spec. Your material and geometry choices should account for this. Key facts:

- Forward axis: +Z (defined above).
- Camera, FOV, distance, and view configuration: see Runtime Spec.
- Lighting: PBR-friendly fixed environment. See Runtime Spec for exact values.
- Background and environment map: see Runtime Spec.

Refer to the Runtime Spec for exact values, integrity hashes, and the canonical render setup before tuning materials or geometry against assumptions.

## Three.js Version

Pinned to **Three.js r183** (npm: `three@0.183.2`). Exact bundle source and integrity hash specified in the Runtime Spec.

## Determinism

Your script must be fully deterministic. Two executions of the same script must produce identical geometry.

- `Math.random()` and `crypto.getRandomValues()` are forbidden. Variation must come from the code your model emits, not runtime randomness.
- All uses of `Date` are forbidden — `Date.now()`, `Date.parse()`, `Date.UTC()`, `new Date()`, and bare `Date` references. Any reference to the identifier `Date` is rejected at parse time.
- `performance.now()` and any reference to the `performance` global are forbidden.
- `MathUtils.seededRandom` and `MathUtils.generateUUID` are forbidden — both rely on stateful or non-deterministic sources.

## Execution Constraints

All limits except wall-clock time and heap usage are checked on the returned root **after** `generate` completes. You may briefly allocate more during construction and then clean up — peak working memory is bounded only by the 256 MB heap cap.

- The combined wall-clock time of module evaluation and the `generate` function call must complete within **5 seconds**. Top-level computation in your module counts against the same 5-second budget. There is no way to offload work to the top level to bypass the limit.
- Maximum **100,000 face counts** total. Computed as the sum of `geometry.attributes.position.count` over every mesh reachable via `root.traverse()`. Geometry shared between meshes is counted once per referencing mesh. For `InstancedMesh`, vertex count is `geometry.position.count` counted once, regardless of instance count.
- Maximum **50 draw calls**. Computed as the number of nodes in `root.traverse()` where `isMesh`, `isLine`, or `isPoints` is true. `InstancedMesh` counts as 1 draw call regardless of instance count. `Group` and bare `Object3D` do not count.
- Maximum scene-graph depth: **16**.
- Maximum total `InstancedMesh` instances across the entire scene: **10,000**.
- Maximum total texture data: **256 KB (262,144 bytes)** across all `DataTexture` instances reachable via materials in the returned scene. Orphan `DataTexture` instances not attached to any rendered material are not counted.
- Maximum JS heap usage during execution: **256 MB**. This cap covers the combined working memory of module evaluation and the `generate` call.
- Bounding box of the returned root must fit within `[-0.5, 0.5]` on all axes. An empty scene (one where `Box3.setFromObject(root).isEmpty() === true`) is treated as failed.

## Code Constraints

- Maximum file size: **1 MB** per script.
- The module must contain exactly one top-level export: the default `generate` function.
- **Total bytes of all literals must not exceed 50 KB.** This includes:
  - All string literal source text (in UTF-8 bytes)
  - All template literal source text (excluding `${}` interpolation expressions)
  - All numeric literal source text, anywhere in the program (not just inside array or typed-array initializers)

  Enforced via AST inspection before execution. Tokens like loop bounds, math constants, and material parameters are counted but are negligible in a normal procedural script. The budget exists to prevent shipping precomputed vertex/index data as code in any form — array literals, object literals, `Object.values`, or any other creative encoding.
- No embedded binary data, base64-encoded assets, or data URIs.
- All 3D content must be constructed through programmatic Three.js API calls.
- No computed property access on `THREE` (e.g. `THREE['Loa' + 'der']` is forbidden). Only direct, statically analyzable property access is allowed.

## Prohibited APIs

- **No network calls:** `fetch`, `XMLHttpRequest`, `WebSocket`, `navigator.sendBeacon`.
- **No DOM access:** `document`, `window`, `navigator`, `localStorage`, `sessionStorage`, `indexedDB`, `HTMLCanvasElement`, `OffscreenCanvas`.
- **No dynamic code execution:** `eval`, `Function`, `setTimeout`, `setInterval`, `setImmediate`, `queueMicrotask`.
- **No randomness:** `Math.random`, `crypto`, `crypto.getRandomValues`.
- **No timing sources:** `Date.now`, `performance.now`, `Date` constructor.
- **No reflection / metaprogramming:** `Proxy`, `Reflect`, `WeakRef`, `FinalizationRegistry`.
- **No concurrency:** `Worker`, `SharedArrayBuffer`, `Atomics`.
- **No imports** beyond the provided `THREE` argument. No `import`, `require`, `import()`.
- **No access** to `process`, `module`, `global`, `globalThis`, `self`.
- **From Three.js MathUtils:** `seededRandom`, `generateUUID`.

## Allowed Three.js APIs

- **Geometry:** `BufferGeometry`, `BufferAttribute`, `InterleavedBuffer`, `InterleavedBufferAttribute`, `Float32BufferAttribute`, `Uint8BufferAttribute`, `Uint16BufferAttribute`, `Uint32BufferAttribute`, `Int8BufferAttribute`, `Int16BufferAttribute`, `Int32BufferAttribute`, all primitive geometries (`BoxGeometry`, `SphereGeometry`, `CylinderGeometry`, `ConeGeometry`, `TorusGeometry`, `TorusKnotGeometry`, `PlaneGeometry`, `CircleGeometry`, `RingGeometry`, `TetrahedronGeometry`, `OctahedronGeometry`, `DodecahedronGeometry`, `IcosahedronGeometry`, `PolyhedronGeometry`), `ExtrudeGeometry`, `LatheGeometry`, `ShapeGeometry`, `TubeGeometry`, `EdgesGeometry`, `WireframeGeometry`.
- **Materials:** `MeshStandardMaterial`, `MeshPhysicalMaterial`, `MeshBasicMaterial`, `PointsMaterial`, `LineBasicMaterial`, `LineDashedMaterial`. Any of these may be passed to any object type — the validator does not enforce pairing. See the note under "Material / object pairing" below.
- **Textures:** `DataTexture` only. Texture data must be generated procedurally in code.
- **Math:** `Vector2`, `Vector3`, `Vector4`, `Matrix3`, `Matrix4`, `Quaternion`, `Euler`, `Box3`, `Sphere`, `Plane`, `Ray`, `Color`, `MathUtils` (excluding `seededRandom` and `generateUUID`).
- **Curves & shapes:** `Curve`, `CurvePath`, `Shape`, `Path`, `CatmullRomCurve3`, `CubicBezierCurve3`, `LineCurve3`, `QuadraticBezierCurve3`.
- **Objects:** `Object3D`, `Group`, `Mesh`, `InstancedMesh`, `Line`, `LineSegments`, `Points`.
- **Constants:** `SRGBColorSpace`, `LinearSRGBColorSpace`, `NoColorSpace`, `FrontSide`, `BackSide`, `DoubleSide`, `NormalBlending`, `AdditiveBlending`, `SubtractiveBlending`, `MultiplyBlending`, `NoBlending`, `NearestFilter`, `LinearFilter`, `NearestMipmapNearestFilter`, `LinearMipmapNearestFilter`, `NearestMipmapLinearFilter`, `LinearMipmapLinearFilter`, `RepeatWrapping`, `ClampToEdgeWrapping`, `MirroredRepeatWrapping`, `RGBAFormat`, `RGBFormat`, `RedFormat`, `UnsignedByteType`, `FloatType`.

### Material / object pairing

Three.js lets you construct any of the objects above (`Mesh`, `InstancedMesh`, `Points`, `Line`, `LineSegments`) with any of the allowed materials, but only some pairings render correctly:

| Object type | Conventional material |
|---|---|
| `Mesh`, `InstancedMesh` | `MeshStandardMaterial`, `MeshPhysicalMaterial`, `MeshBasicMaterial` |
| `Points` | `PointsMaterial` |
| `Line`, `LineSegments` | `LineBasicMaterial`, `LineDashedMaterial` |

Mis-paired combinations (e.g., `Points` with `MeshBasicMaterial`, `Mesh` with `PointsMaterial`) **are not rejected by the validator** — they pass every stage and the script runs to completion. But they render incorrectly: `Mesh` with `PointsMaterial` draws no visible pixels because mesh shaders don't set `gl_PointSize`; `Points` with a mesh material renders as a single point per draw call at default 1-pixel size. The rendered frame is what the VLM judge scores against the prompt image, so a mis-paired material costs you quality points even though it doesn't cost you a validator failure. Use the conventional pairings in the table above unless you have a specific visual reason not to.

## Prohibited Three.js APIs

- Any loader: `GLTFLoader`, `OBJLoader`, `TextureLoader`, `FileLoader`, `ImageLoader`, etc.
- `ShaderMaterial`, `RawShaderMaterial` (to prevent hiding baked assets in shader code).
- `MeshLambertMaterial`, `MeshPhongMaterial` (non-PBR materials render inconsistently under the fixed lighting setup).
- `CanvasTexture`, `VideoTexture`, `CompressedTexture`, `CubeTexture`, base `Texture` constructor (require DOM or external assets).
- `AnimationMixer`, `AnimationClip`, `KeyframeTrack` and all subclasses.
- `Skeleton`, `SkinnedMesh`, `Bone`, morph targets (`geometry.morphAttributes`).
- Any method that accepts URLs or file paths.

## Failure Semantics

| Condition | Result | Rule Code |
|-----------|--------|-----------|
| File size exceeds 1 MB | Rejected at parse time, failed | `FILE_SIZE_EXCEEDED` |
| Module has no `export default` function | Rejected at parse time, failed | `MISSING_DEFAULT_EXPORT` |
| Module has more than one top-level export | Rejected at parse time, failed | `MULTIPLE_TOP_LEVEL_EXPORTS` |
| `generate` is async or returns a `Promise` | Rejected at parse time, failed | `ASYNC_NOT_ALLOWED` |
| Identifier outside the allowlist | Rejected before execution, failed | `IDENTIFIER_NOT_ALLOWED` |
| Forbidden identifier referenced (e.g. `fetch`, `Date`) | Rejected before execution, failed | `FORBIDDEN_IDENTIFIER` |
| Forbidden Three.js API used (e.g. `THREE.AnimationMixer`) | Rejected before execution, failed | `FORBIDDEN_THREE_API` |
| Top-level reference to `THREE` | Rejected before execution, failed | `THREE_AT_TOP_LEVEL` |
| Computed property access on `THREE` or other gated globals | Rejected before execution, failed | `COMPUTED_PROPERTY_ACCESS` |
| Literal byte budget exceeded (50 KB) | Rejected before execution, failed | `LITERAL_BUDGET_EXCEEDED` |
| Function throws an exception | Failed | `EXECUTION_THREW` |
| Function returns `null`, `undefined`, or unsupported type | Failed | `INVALID_RETURN_TYPE` |
| Function returns `Mesh`, `LineSegments`, or `Points` | Wrapped in a `Group` automatically, accepted | (none) |
| Function returns `Group` | Accepted as-is | (none) |
| Function returns an empty scene (`Box3.setFromObject(root).isEmpty()`) | Failed | `EMPTY_SCENE` |
| Execution exceeds 5-second combined timeout | Killed, failed | `TIMEOUT_EXCEEDED` |
| JS heap exceeds 256 MB during execution | Killed, failed | `HEAP_EXCEEDED` |
| Object hierarchy contains cycles | Killed during traversal, failed | `CYCLE_DETECTED` |
| Face count exceeds 100,000 | Failed | `VERTEX_LIMIT_EXCEEDED` |
| Draw call count exceeds 50 | Failed | `DRAW_CALL_LIMIT_EXCEEDED` |
| Scene-graph depth exceeds 16 | Failed | `DEPTH_LIMIT_EXCEEDED` |
| Instance count exceeds 10,000 | Failed | `INSTANCE_LIMIT_EXCEEDED` |
| Texture data exceeds 256 KB | Failed | `TEXTURE_DATA_EXCEEDED` |
| Bounding box exceeds `[-0.5, 0.5]` on any axis | Failed | `BOUNDING_BOX_OUT_OF_RANGE` |
| Render run failed after passing validation | Failed | `RENDER_RUN_FAILED` |

Each rule code is paired with a failure stage and detail in the structured error returned to miners. See **Runtime Specification § Error Reporting** for the JSON shape and the stage that emits each code.

Failed prompts within a batch do not block the rest of the batch. Other prompts in the same batch are scored normally.

## Validation

All submissions undergo automated static analysis before execution:

1. **Parse** the script with an AST parser. Reject on syntax errors, missing default export, or multiple top-level exports.
2. **Identifier allowlist.** Only allowlisted identifiers may appear. Computed property access on `THREE` and other restricted globals is rejected.
3. **Literal byte budget.** Sum bytes of all literals. Reject if total exceeds 50 KB.
4. **Forbidden API check.** Reject any reference to prohibited identifiers.

After successful execution, the validator computes `new THREE.Box3().setFromObject(root)` and rejects assets where any axis falls outside `[-0.5, 0.5]` or where the box is empty. Vertex count, draw call count, scene-graph depth, instance count, and texture data limits are all checked at this stage on the returned root.

Code that passes both static and post-execution validation is then handed to the rendering and judging pipeline described in the Runtime Spec.

## See Also

- **Runtime Specification** — sandbox model, execution environment, exact Three.js bundle, AST allowlist details, camera/lighting/render setup.
- **API Specification** — `/generate`, `/status`, `/results` endpoints for batch processing.
- **Competition Rules** — scoring, prompt sets, time budgets, pod management.

<a id="tldr"></a>

## TL;DR

- **Module:**
  - One ES module — default export `function generate(THREE)` only, synchronous, no imports.
  - `THREE` exists only as that parameter (nothing at module top level).
- **Result:**
  - Return a `Group`, `Mesh`, `LineSegments`, or `Points` inside `[-0.5, 0.5]` on every axis (centered, Y-up, **+Z** forward).
  - You choose geometry/materials; the validator owns camera, resolution, lights, background, and environment.
- **Determinism & APIs:**
  - No randomness, `Date`, `performance`, or other banned sources (see § Determinism and § Prohibited APIs).
  - Only allowed `THREE.*` usage (§ Allowed Three.js APIs); everything else is rejected.
- **Budgets:**
  - File ≤ **1 MB**; literals ≤ **50 KB**.
  - Combined module evaluation + `generate()` within **5 s** wall-clock and **256 MB** heap.
  - Plus scene limits in § Execution Constraints (faces, draws, depth, instances, `DataTexture` bytes).
- **Failures:**
  - Each rejection maps to a **rule code** in § Failure Semantics.
  - Structured `{ stage, rule, detail }` responses are described in **Runtime Spec § Error Reporting**.