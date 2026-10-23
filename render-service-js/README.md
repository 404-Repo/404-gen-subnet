# render-service-js

Renders miner Three.js submissions to multi-view PNG images via headless Chromium with WebGL.

Designed to run on RunPod Serverless GPU workers. Three-stage pipeline per request:

1. **Static analysis** — parse + AST rules (fast, synchronous)
2. **Execution + post-validation** — runs `generate(THREE)` in a `worker_threads` pool with V8 heap limits (256 MB) and wall-clock timeout (5 s), then validates the returned scene (bounding box, vertex count, draw calls, depth, instances, texture data)
3. **Render** — only validated submissions reach Chromium; each render gets a fresh BrowserContext from a bounded render slot pool

## Local development

Requires Node.js >= 20.

```bash
cd render-service-js
npm install
npm run dev    # starts with --watch for auto-reload
```

The server starts on `http://localhost:80` by default (set `PORT=8000` for local dev).

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `80` | HTTP server port |
| `STATIC_PORT` | `3000` | Internal static file server port (serves Three.js to Chromium) |
| `RENDER_TIMEOUT_MS` | `10000` | Max time for a single render before timeout |
| `VALIDATION_POOL_SIZE` | `2` | Number of worker_threads for execution + post-validation |
| `VALIDATION_TIMEOUT_MS` | `5000` | Wall-clock timeout for miner code execution (spec: 5 s) |
| `RENDER_POOL_SIZE` | `2` | Max concurrent browser render slots |
| `USE_GL` | _(unset, uses ANGLE)_ | Chromium GL backend. Set to `egl` on RunPod/NVIDIA GPU workers |

## Docker

```bash
docker build -t render-service-js .
docker run -p 8000:8000 render-service-js
```

For RunPod GPU workers with NVIDIA:

```bash
docker run --gpus all -e USE_GL=egl -p 8000:8000 render-service-js
```

## API

### Request body

All render endpoints accept a JSON body with either:

- `{"source": "export default function generate(THREE) { ... }"}` -- inline JS source
- `{"url": "https://example.com/submission.js"}` -- URL to fetch JS source from

The source must export a default function that receives `THREE` and returns an `Object3D`.

### Query parameters

All render endpoints accept the same query parameters for camera control:

| Parameter | Default | Description |
|---|---|---|
| `thetas` | `24,120,216,312` | Comma-separated azimuth angles in degrees |
| `phis` | `-15` | Comma-separated elevation angles (single value applies to all views) |
| `img_size` | `518` | Width and height of each view in pixels |
| `cam_radius` | `2.0` | Camera distance from origin |
| `cam_fov_deg` | `49.1` | Camera vertical field of view in degrees |
| `gap` | `5` | Pixel gap between views in grid mode |
| `bg_color` | `808080` | Background color as hex string (e.g. `ffffff` for white) |
| `lighting` | `studio` | Lighting mode: `studio` (fixed key/fill/rim rig + env map) or `follow` (camera-following single light) |

### `POST /render`

Renders individual views. Returns different formats depending on the number of views:

**Single view** (e.g. `?thetas=45`): returns a raw PNG.

```bash
curl -X POST 'http://localhost:8000/render?thetas=45' \
  -H 'Content-Type: application/json' \
  -d '{"source": "export default function generate(THREE) { ... }"}' \
  -o view.png
```

**Multiple views** (e.g. `?thetas=24,120,216,312` or default): returns JSON with base64-encoded PNGs.

```bash
curl -X POST 'http://localhost:8000/render' \
  -H 'Content-Type: application/json' \
  -d '{"source": "export default function generate(THREE) { ... }"}'
```

```json
{
  "images": ["iVBORw0KGgo...", "iVBORw0KGgo...", "iVBORw0KGgo...", "iVBORw0KGgo..."],
  "count": 4,
  "img_size": 518
}
```

### `POST /render/grid`

Renders all views and stitches them into a single composite PNG (2x2 grid for 4 views).

```bash
curl -X POST 'http://localhost:8000/render/grid' \
  -H 'Content-Type: application/json' \
  -d '{"source": "export default function generate(THREE) { ... }"}' \
  -o grid.png
```

Always returns a raw PNG.

### `GET /health`

```json
{"status": "ok"}
```

### `GET /ping`

Returns `200` when the service is ready, `204` while still initializing.

### Error responses

**422** -- static analysis validation failed:

```json
{
  "error": "validation_failed",
  "failures": [
    {"stage": "static_analysis", "rule": "FORBIDDEN_IDENTIFIER", "detail": "Math.random at line 3"}
  ]
}
```

**422** -- post-execution validation failed (code ran but scene violates limits):

```json
{
  "error": "post_validation_failed",
  "failures": [
    {"stage": "post_validation", "rule": "VERTEX_LIMIT_EXCEEDED", "detail": "312000 > 250000"}
  ],
  "metrics": {
    "vertices": 312000,
    "drawCalls": 5,
    "depth": 2,
    "instances": 0,
    "textureBytes": 0,
    "bbox": {"min": {"x": -0.5, "y": -0.5, "z": -0.5}, "max": {"x": 0.5, "y": 0.5, "z": 0.5}}
  }
}
```

Static-analysis rule codes include `FORBIDDEN_THREE_API` (real Three.js API that's deliberately disallowed), `UNKNOWN_THREE_API` (referenced `THREE.X` where X is not a real Three.js member — typo/hallucination), and `THREE_ALIAS_FORBIDDEN` (the `THREE` parameter escaped its allowlist-tracked positions). `THREE_ALIAS_FORBIDDEN` covers direct aliasing (`const X = THREE`), spread / rest / array destructure, stashing in containers (`{t: THREE}`, `[THREE]`), returning from helpers (`() => THREE`), and forwarding to a callee whose receiving parameter is renamed, destructured, a rest parameter, a default `= THREE`, or unresolvable (method dispatch, dynamic calls). Named destructure like `const { Group, Mesh } = THREE` is allowed; each extracted name is validated against the allowlist in the same pass. Helpers that genuinely need `THREE` must accept it under the exact parameter name `THREE` (`function fitToUnitCube(THREE, root)` keeps working).

Post-validation rule codes: `EMPTY_SCENE`, `BOUNDING_BOX_OUT_OF_RANGE`, `VERTEX_LIMIT_EXCEEDED`, `DRAW_CALL_LIMIT_EXCEEDED`, `DEPTH_LIMIT_EXCEEDED`, `INSTANCE_LIMIT_EXCEEDED`, `TEXTURE_DATA_EXCEEDED`, `CYCLE_DETECTED`, `INVALID_RETURN_TYPE`, `TIMEOUT_EXCEEDED`, `HEAP_EXCEEDED`, `EXECUTION_THREW`. The texture-data cap is **4 MB**.

**500** -- render or internal error:

```json
{"error": "Error creating WebGL context."}
```

## PMREM cache

The fixed `neutral` and `studio` lighting environments are baked once per
Chromium instance, on first use, and cached in memory. Baking runs in a trusted,
disposable context without miner code. Each miner render gets a fresh
BrowserContext and a private texture. `follow` lighting does not use PMREM.

Concurrent requests share the same pending bake. A failed bake is evicted so a
later request can retry. Replacing the browser or restarting the service clears
the cache. Pixels are cached only after a completed readback with no WebGL errors
or context loss. The first request for each environment includes the baking cost.

Readback failures return HTTP 500. The service stays available, and the next
request can rebuild the failed entry.

### Tests

`npm test` checks cache reuse and lifecycle. `npm run test:pmrem` checks WebGL and
HTTP failure recovery and compares complete PNG bytes against a reference
renderer. The HTTP recovery cases use a 60-second render deadline.

Use the pinned `linux/amd64` validation image with Node `24.14.0`, Puppeteer
`24.40.0`, Three.js `0.183.2` and Chrome `146.0.7680.153`. This image must already
be loaded locally; building the Dockerfile above produces a different runtime.
Run both implementations with the image's locked dependencies. From the
repository root:

```sh
PMREM_IMAGE_ID=sha256:4e8ea7a0ffe1f59d93ef489df009818ede0f92ef1438aa7d9096073c51cc3ea4
PMREM_REFERENCE_DIR=$(mktemp -d)
git archive c4dc226 render-service-js/src | tar -x -C "$PMREM_REFERENCE_DIR"
docker run --rm --init --pull=never --platform linux/amd64 \
  --cpus=2 --memory=2g --pids-limit=512 --network none \
  -e RENDER_POOL_SIZE=2 -e PMREM_REFERENCE_DIR=/app/reference \
  -e PMREM_IMAGE_ID="$PMREM_IMAGE_ID" \
  --mount "type=bind,src=$PWD/render-service-js/src,dst=/app/src,readonly" \
  --mount "type=bind,src=$PWD/render-service-js/test,dst=/app/test,readonly" \
  --mount "type=bind,src=$PMREM_REFERENCE_DIR/render-service-js,dst=/app/reference,readonly" \
  --mount "type=bind,src=$PWD/miner-reference/examples,dst=/fixtures,readonly" \
  --mount "type=bind,src=$PWD/render-service-js/package.json,dst=/app/package.json,readonly" \
  --mount "type=bind,src=$PWD/render-service-js/package-lock.json,dst=/app/package-lock.json,readonly" \
  --entrypoint npm "$PMREM_IMAGE_ID" run test:pmrem
```

To save PNGs and a JSON report with source hashes, output hashes and timings,
mount an output directory and set `PMREM_TEST_OUTPUT` to its container path.
Render failures identify the case and stage and preserve the original error.
Byte identity is checked within the chosen runtime; this does not establish
pixel determinism between different hosts or browser versions.

## Security

Each submission passes through four isolation stages, layered so each
catches the kind of attack the layer above was not shaped for. **The
runtime `safeTHREE` Proxy is the canonical security boundary for Three.js
API capability filtering; static analysis remains as a fast-fail ergonomic
layer that gives miners clear errors on well-formed code.**

1. **Static analysis** — AST-level rejection of forbidden identifiers,
   imports, computed property access, literal budget violations, and a
   family of `THREE`-laundering patterns (see `FORBIDDEN_THREE_API` /
   `UNKNOWN_THREE_API` / `THREE_ALIAS_FORBIDDEN` in the rule-code list).
   This is the friendly layer: it gives miners precise, line-accurate
   errors before code runs. It is also the layer that has been shown to
   miss syntactic corners twice — so the Proxy below is what we actually
   rely on for capability enforcement.
2. **Runtime `safeTHREE` Proxy** — miner code never receives the real
   `THREE` namespace. It receives a Proxy (`src/safeThree.js`) whose `get`
   trap returns only allowlisted members and throws on everything else, a
   `MathUtils` sub-Proxy that blocks `seededRandom` / `generateUUID`, and
   mutation traps that reject `set` / `delete` / `defineProperty` on the
   namespace itself. Because every property access on a Proxy goes through
   the `get` trap, there is no JavaScript syntax — alias, destructure,
   helper forwarding, container stashing, reflective enumeration, or any
   future form — that can reach a forbidden API through this value.
   Enforced by `test/runtime-adversary.test.mjs`, which re-runs every
   known attack pattern against the runtime and asserts each throws.
3. **Execution in worker_threads** — miner code runs in a dedicated V8
   thread with `resourceLimits` (256 MB heap), 5 s wall-clock timeout,
   seeded PRNG replacing `Math.random`, and trapped globals (`eval`,
   `Function`, `Date`, network APIs, …). The returned scene is validated
   against spec limits (bounding box, vertices, draw calls, depth,
   instances, textures). Workers are terminated and replaced on timeout or
   crash.
4. **Render in Chromium** — only validated code reaches the browser. Each
   render gets a fresh BrowserContext (isolated JS heap). The in-page
   runtime also wraps `window.THREE` with the same safeTHREE Proxy before
   trapping globals, so the capability boundary applies consistently to
   both the Node-side validator and the browser-side renderer. Per-slot
   recovery: a failed render closes only that context, not the whole
   browser.
