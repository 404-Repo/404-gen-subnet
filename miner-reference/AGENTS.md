# Three.js Generation — Rules Reference

Compact reference for code-generation LLMs. Every rule here is enforced by static analysis, sandbox execution, or post-execution validation. Code that violates any rule is rejected.

## Function signature

```js
export default function generate(THREE) {
  const root = new THREE.Group();
  // build geometry, materials, textures here
  fitToUnitCube(THREE, root);
  return root;
}
```

- The module must have exactly **one** export: the default function.
- The function must be **synchronous** (no `async`, no returning a Promise).
- `THREE` is the **only** API available — passed as the function parameter, not a global.
- Top-level code **cannot** reference `THREE`. Helper functions must receive it as a parameter.
- No `import` or `require` statements. No external dependencies.
- Return a `Group`, `Mesh`, `LineSegments`, or `Points`. Returning `null`/`undefined`/other types fails.

## Limits

| Resource | Limit |
|----------|-------|
| Vertices (sum of `geometry.attributes.position.count`) | 250,000 |
| Draw calls (nodes where `isMesh`/`isLine`/`isPoints`) | 200 |
| Scene-graph depth | 32 |
| InstancedMesh instances (sum of `.count`) | 50,000 |
| Texture data (all `DataTexture` buffers via materials) | 4,194,304 bytes (4 MB) |
| Bounding box (all axes) | [-0.5, 0.5] |
| Execution time (module eval + `generate()` call) | 5,000 ms |
| JS heap | 256 MB |
| File size | 1 MB |
| Total literal bytes (strings + numbers + template quasis) | 50 KB |

The 50 KB literal budget prevents embedding precomputed vertex data. All geometry must be constructed algorithmically.

## Coordinate system

- Y-up orientation.
- **+Z is the forward axis.** The camera looks down -Z, so models should face +Z.
- Origin at the geometric center of the asset.
- Use the `fitToUnitCube` pattern (see below) to normalize into [-0.5, 0.5].

## Determinism

Code must be fully deterministic — two executions produce identical geometry.

Forbidden:
- `Math.random()` — use algorithmic variation derived from position, index, or constants.
- `Date`, `Date.now()`, `new Date()` — all `Date` references rejected at parse time.
- `performance.now()`, `performance` global.
- `THREE.MathUtils.seededRandom`, `THREE.MathUtils.generateUUID`.
- `crypto`, `crypto.getRandomValues`.

## Allowed Three.js APIs

Everything accessed via `THREE.X` is checked against this allowlist. Anything not listed is rejected.

**Geometry:** `BufferGeometry`, `BufferAttribute`, `InterleavedBuffer`, `InterleavedBufferAttribute`, `Float32BufferAttribute`, `Uint8BufferAttribute`, `Uint16BufferAttribute`, `Uint32BufferAttribute`, `Int8BufferAttribute`, `Int16BufferAttribute`, `Int32BufferAttribute`, `BoxGeometry`, `SphereGeometry`, `CylinderGeometry`, `CapsuleGeometry`, `ConeGeometry`, `TorusGeometry`, `TorusKnotGeometry`, `PlaneGeometry`, `CircleGeometry`, `RingGeometry`, `TetrahedronGeometry`, `OctahedronGeometry`, `DodecahedronGeometry`, `IcosahedronGeometry`, `PolyhedronGeometry`, `ExtrudeGeometry`, `LatheGeometry`, `ShapeGeometry`, `TubeGeometry`, `EdgesGeometry`, `WireframeGeometry`.

**Materials:** `MeshStandardMaterial`, `MeshPhysicalMaterial`, `MeshBasicMaterial`, `PointsMaterial`, `LineBasicMaterial`, `LineDashedMaterial`.

Material / object pairing is **not enforced by the validator** but it matters for what renders. Use mesh materials (`MeshStandardMaterial` / `MeshPhysicalMaterial` / `MeshBasicMaterial`) on `Mesh` and `InstancedMesh`. Use `PointsMaterial` on `Points`. Use `LineBasicMaterial` or `LineDashedMaterial` on `Line` and `LineSegments`. Mis-pairings pass validation but render incorrectly (e.g., `Mesh` with `PointsMaterial` draws nothing because mesh shaders don't set `gl_PointSize`), costing VLM judging points.

**Textures:** `DataTexture` only. Data must be generated procedurally in code.

**Math:** `Vector2`, `Vector3`, `Vector4`, `Matrix3`, `Matrix4`, `Quaternion`, `Euler`, `Box2`, `Box3`, `Sphere`, `Plane`, `Ray`, `Line3`, `Triangle`, `Spherical`, `Cylindrical`, `Color`, `MathUtils` (excluding `seededRandom` and `generateUUID`).

**Curves & shapes:** `Curve`, `CurvePath`, `Shape`, `Path`, `CatmullRomCurve3`, `CubicBezierCurve3`, `LineCurve3`, `QuadraticBezierCurve3`, `EllipseCurve`, `ArcCurve`, `LineCurve`, `SplineCurve`, `QuadraticBezierCurve`, `CubicBezierCurve`.

**Objects:** `Object3D`, `Group`, `Mesh`, `InstancedMesh`, `Line`, `LineSegments`, `Points`.

**Constants:** `SRGBColorSpace`, `LinearSRGBColorSpace`, `NoColorSpace`, `FrontSide`, `BackSide`, `DoubleSide`, `NormalBlending`, `AdditiveBlending`, `SubtractiveBlending`, `MultiplyBlending`, `NoBlending`, `NearestFilter`, `LinearFilter`, `NearestMipmapNearestFilter`, `LinearMipmapNearestFilter`, `NearestMipmapLinearFilter`, `LinearMipmapLinearFilter`, `RepeatWrapping`, `ClampToEdgeWrapping`, `MirroredRepeatWrapping`, `RGBAFormat`, `RGBFormat`, `RGFormat`, `RedFormat`, `LuminanceFormat`, `UnsignedByteType`, `HalfFloatType`, `FloatType`, `StaticDrawUsage`, `DynamicDrawUsage`, `StreamDrawUsage`.

## Prohibited APIs

These identifiers are rejected anywhere in the code (even as local variable names):

`eval`, `Function`, `setTimeout`, `setInterval`, `setImmediate`, `queueMicrotask`, `fetch`, `XMLHttpRequest`, `WebSocket`, `document`, `window`, `navigator`, `localStorage`, `sessionStorage`, `indexedDB`, `OffscreenCanvas`, `HTMLCanvasElement`, `crypto`, `Date`, `performance`, `Proxy`, `Reflect`, `WeakRef`, `FinalizationRegistry`, `SharedArrayBuffer`, `Atomics`, `Worker`, `process`, `module`, `global`, `globalThis`, `self`, `require`, `import`.

Prohibited Three.js APIs: any loader (`GLTFLoader`, `TextureLoader`, etc.), `ShaderMaterial`, `RawShaderMaterial`, `MeshLambertMaterial`, `MeshPhongMaterial`, `CanvasTexture`, `VideoTexture`, `CompressedTexture`, `CubeTexture`, `AnimationMixer`, `AnimationClip`, `KeyframeTrack`, `Skeleton`, `SkinnedMesh`, `Bone`, morph targets.

No computed property access on `THREE`: `THREE['Box' + 'Geometry']` is rejected. Only direct access like `THREE.BoxGeometry`.

## Allowed globals (top-level code only)

`Math`, `Number`, `String`, `Array`, `Object`, `Symbol`, `JSON`, `Map`, `Set`, `Boolean`, `Error`, `TypeError`, `RangeError`, `Infinity`, `NaN`, `undefined`, `parseInt`, `parseFloat`, `isFinite`, `isNaN`, `BigInt`, `ArrayBuffer`, `DataView`, `Int8Array`, `Uint8Array`, `Uint8ClampedArray`, `Int16Array`, `Uint16Array`, `Int32Array`, `Uint32Array`, `Float32Array`, `Float64Array`, `BigInt64Array`, `BigUint64Array`.

Plus any identifiers declared locally (`const`, `let`, `var`, function parameters).

## Rendering context

The asset is rendered under a fixed setup you cannot change. Tune materials accordingly.

- **Camera:** `PerspectiveCamera`, FOV 17.5°, position `(0, 0.3, 3.5)`, target `(0, 0, 0)`, Y-up
- **Key light:** `DirectionalLight(0xffffff, 1.2)` at `(2, 3, 2)`
- **Fill light:** `DirectionalLight(0xffffff, 0.4)` at `(-2, 1, 1)`
- **Ambient:** `AmbientLight(0xffffff, 0.15)`
- **Environment:** Neutral studio HDRI, intensity 1.0
- **Background:** Solid gray `0x808080`
- **Renderer:** 1024×1024, ACES filmic tone mapping, sRGB output, pixel ratio 1
- **Three.js version:** `three@0.183.2` (r183)

## Essential patterns

### fitToUnitCube — bounding box normalization

Build geometry at natural scale, then normalize the root to fit [-0.5, 0.5]:

```js
function fitToUnitCube(THREE, root) {
  const box = new THREE.Box3().setFromObject(root);
  const size = new THREE.Vector3();
  const center = new THREE.Vector3();
  box.getSize(size);
  box.getCenter(center);
  const maxDim = Math.max(size.x, size.y, size.z);
  const scale = 0.95 / maxDim;
  root.scale.setScalar(scale);
  root.position.set(-center.x * scale, -center.y * scale, -center.z * scale);
}
```

### Procedural DataTexture

```js
function makeTexture(THREE, size) {
  const data = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      data[i] = /* R */; data[i+1] = /* G */; data[i+2] = /* B */; data[i+3] = 255;
    }
  }
  const tex = new THREE.DataTexture(data, size, size);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  return tex;
}
```

If a material uses a `DataTexture` via `map`, the geometry must have a `uv` attribute. Primitive geometries (`BoxGeometry`, `SphereGeometry`, etc.) provide UVs automatically; custom `BufferGeometry` does not.

### Reuse geometry and materials

Create a geometry or material once, reference it from multiple meshes. Reduces memory and keeps draw call count low. `InstancedMesh` is ideal for repeated elements (leaves, bricks, bolts):

```js
const geo = new THREE.BoxGeometry(0.1, 0.1, 0.1);
const mat = new THREE.MeshStandardMaterial({ color: 0x886644 });
const im = new THREE.InstancedMesh(geo, mat, count);
const dummy = new THREE.Object3D();
for (let i = 0; i < count; i++) {
  dummy.position.set(/* ... */);
  dummy.updateMatrix();
  im.setMatrixAt(i, dummy.matrix);
}
```

## Complete example — low-poly car

```js
export default function generate(THREE) {
  const car = new THREE.Group();

  const bodyMat = new THREE.MeshStandardMaterial({ color: 0xcc0000, metalness: 0.6, roughness: 0.4 });
  const cabinMat = new THREE.MeshStandardMaterial({ color: 0x222244, metalness: 0.2, roughness: 0.3 });
  const wheelMat = new THREE.MeshStandardMaterial({ color: 0x111111, metalness: 0.1, roughness: 0.8 });

  const body = new THREE.Mesh(new THREE.BoxGeometry(2.0, 1.2, 4.0), bodyMat);
  body.position.set(0, 0.6, 0);
  car.add(body);

  const cabin = new THREE.Mesh(new THREE.BoxGeometry(1.8, 1.0, 2.0), cabinMat);
  cabin.position.set(0, 1.7, -0.3);
  car.add(cabin);

  const wheelGeo = new THREE.CylinderGeometry(0.4, 0.4, 0.3, 16);
  const offsets = [[1.1, 0, 1.3], [-1.1, 0, 1.3], [1.1, 0, -1.3], [-1.1, 0, -1.3]];
  for (const o of offsets) {
    const wheel = new THREE.Mesh(wheelGeo, wheelMat);
    wheel.position.set(o[0], o[1], o[2]);
    wheel.rotation.z = Math.PI / 2;
    car.add(wheel);
  }

  fitToUnitCube(THREE, car);
  return car;
}

function fitToUnitCube(THREE, root) {
  const box = new THREE.Box3().setFromObject(root);
  const size = new THREE.Vector3();
  const center = new THREE.Vector3();
  box.getSize(size);
  box.getCenter(center);
  const maxDim = Math.max(size.x, size.y, size.z);
  const scale = 0.95 / maxDim;
  root.scale.setScalar(scale);
  root.position.set(-center.x * scale, -center.y * scale, -center.z * scale);
}
```

## Failure codes

When validation fails, the error has the shape `{ stage, rule, detail }`. Common codes:

| Rule Code | Cause |
|-----------|-------|
| `MISSING_DEFAULT_EXPORT` | No `export default function` |
| `ASYNC_NOT_ALLOWED` | `async` function or Promise return |
| `FORBIDDEN_IDENTIFIER` | Used `fetch`, `Date`, `window`, etc. |
| `IDENTIFIER_NOT_ALLOWED` | Used an identifier not on the globals allowlist |
| `FORBIDDEN_THREE_API` | Used a real Three.js API that is deliberately disallowed (e.g. `THREE.AnimationMixer`) |
| `UNKNOWN_THREE_API` | Referenced `THREE.X` where X is not a real Three.js member (typo or hallucination) |
| `THREE_ALIAS_FORBIDDEN` | Rebound `THREE` to another name or let it escape through a function boundary. Covers aliasing (`const X = THREE`), spread (`{ ...THREE }`, `foo(...THREE)`), array / rest destructure (`const [a] = THREE`, `const { ...r } = THREE`), stash-in-container (`{ t: THREE }`, `[THREE]`), return-from-helper (`() => THREE`), passing `THREE` to a helper whose parameter is renamed / destructured / rest / defaulted (`use((t) => ...)`, `use(({X}) => ...)`, `use(...args)`, `use((t = THREE) => ...)`), passing `THREE` via method or dynamic dispatch (`f.call(null, THREE)`, `obj.method(THREE)`). Access members only as `THREE.X` or via named destructure (`const { Group } = THREE`); helpers that need `THREE` must declare the parameter with the exact name `THREE`. |
| `THREE_AT_TOP_LEVEL` | Referenced `THREE` outside `generate()` body |
| `COMPUTED_PROPERTY_ACCESS` | Used `THREE[expr]` instead of `THREE.Name` |
| `LITERAL_BUDGET_EXCEEDED` | Total literal bytes > 50 KB |
| `EXECUTION_THREW` | `generate()` threw an exception |
| `INVALID_RETURN_TYPE` | Returned wrong type or null |
| `TIMEOUT_EXCEEDED` | Exceeded 5 s wall-clock |
| `VERTEX_LIMIT_EXCEEDED` | More than 250,000 vertices |
| `DRAW_CALL_LIMIT_EXCEEDED` | More than 200 draw calls |
| `DEPTH_LIMIT_EXCEEDED` | Scene graph deeper than 32 |
| `INSTANCE_LIMIT_EXCEEDED` | More than 50,000 instances |
| `TEXTURE_DATA_EXCEEDED` | More than 4 MB texture data |
| `BOUNDING_BOX_OUT_OF_RANGE` | Asset outside [-0.5, 0.5] |
| `EMPTY_SCENE` | Bounding box is empty |
