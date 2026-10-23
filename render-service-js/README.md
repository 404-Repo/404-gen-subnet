# render-service-js

Renders miner Three.js submissions to multi-view PNG images via headless Chromium with WebGL.

Each request passes through three stages:

1. **Static analysis** — parse + AST rules (fast, synchronous)
2. **Execution + post-validation** — runs `generate(THREE)` in a `worker_threads` pool with V8 heap limits (256 MB) and wall-clock timeout (5 s), then validates the returned scene (bounding box, vertex count, draw calls, depth, instances, texture data)
3. **Render** — only validated submissions reach Chromium; each render gets a fresh BrowserContext from a bounded render slot pool

## Local development

Requires Node.js >= 20.

```bash
cd render-service-js
npm ci
PORT=8000 npm run dev
npm test
```

The development server above listens on `http://localhost:8000`. The default port is `80`.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `80` | HTTP server port |
| `STATIC_PORT` | `3000` | Internal static file server port (serves Three.js to Chromium) |
| `RENDER_TIMEOUT_MS` | `30000` | Max time for a single render before timeout |
| `VALIDATION_POOL_SIZE` | `2` | Number of worker_threads for execution + post-validation |
| `VALIDATION_TIMEOUT_MS` | `5000` | Wall-clock timeout for miner code execution (spec: 5 s) |
| `RENDER_POOL_SIZE` | `1` | Max concurrent browser render slots |

## Docker

```bash
docker build -t render-service-js .
docker run -p 8000:80 render-service-js
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
| `lighting` | `studio` | `studio` (fixed lights), `neutral` (luminous environment), or `follow` (camera-following light) |

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

Both render endpoints use these responses:

| Status | `error` | Meaning |
|---|---|---|
| 422 | `validation_failed` | Static validation failed |
| 422 | `post_validation_failed` | Execution or scene validation failed |
| 503 | `validation_unavailable` | Unexpected validation worker failure or unavailable pool |
| 500 | Error message | Render or internal error |

Generator exceptions, execution timeouts and confirmed V8 heap-limit failures
are 422 rejections. Validation reports contain at most 50 failures, 2,048 UTF-8
bytes per detail and 64 KiB of JSON. `truncated` is true when diagnostic data
is omitted.

```json
{
  "error": "post_validation_failed",
  "failures": [
    {"stage": "execution", "rule": "EXECUTION_THREW", "detail": "boom"}
  ],
  "metrics": null,
  "truncated": false
}
```

Post-execution reports include scene metrics when available, otherwise `null`.
Execution rules include `EXECUTION_THREW`, `RUNTIME_VIOLATION`,
`TIMEOUT_EXCEEDED` and `HEAP_EXCEEDED`. See the
[output specification](../miner-reference/output_specifications.md) for scene
limits and static-validation rules.

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

Static analysis checks syntax restrictions and Three.js API references. The
runtime `safeTHREE` proxy exposes allowlisted members in both Node and Chromium.

Validation runs in worker threads with a 256 MB V8 heap limit and a five-second
deadline by default. JavaScript intrinsics and shared Three.js exports are
read-only; instance-owned geometry, materials and callbacks remain mutable.
Uncaught mutation errors return 422 execution rejections. Workers are terminated
and replaced after a timeout or crash.

Each render uses a fresh BrowserContext and a bounded render slot. A failed
render closes its context. An unhealthy browser is restarted on the next request.
