# render-service-js

Renders miner Three.js submissions to multi-view PNG images via headless Chromium with WebGL.

Designed to run on RunPod Serverless GPU workers. Each request gets an isolated BrowserContext; if a render fails, Chromium is restarted automatically.

## Local development

Requires Node.js >= 20.

```bash
cd render-service-js
npm install
npm run dev    # starts with --watch for auto-reload
```

The server starts on `http://localhost:8000` by default.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `80` | HTTP server port |
| `STATIC_PORT` | `3000` | Internal static file server port (serves Three.js to Chromium) |
| `RENDER_TIMEOUT_MS` | `10000` | Max time for a single render before timeout |
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

**500** -- render or internal error:

```json
{"error": "Error creating WebGL context."}
```

## Security

Before rendering, each submission is run through the static analysis validator (from `miner-reference/validator`). This rejects code that references forbidden APIs like `Math.random`, `Date`, `performance`, `fetch`, `XMLHttpRequest`, etc.

Each render runs in a fresh Puppeteer BrowserContext (isolated JS heap, cookies, storage). If rendering fails or times out, the entire Chromium process is restarted.
