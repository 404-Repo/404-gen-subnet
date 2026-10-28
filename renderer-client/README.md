# RendererClient

Thin synchronous and async Python clients for the existing Three.js renderer. JavaScript,
Chromium, validation and PNG generation stay in `render-service-js`. The client
depends only on HTTPX; it does not start containers or install a browser.

## Miner installation and Pod rendering

The launch package is `404-renderer-client==0.1.3rc1`, paired with
`404-witness-client==0.2.13rc9` from source tag `witness-client-v0.2.13rc9`.
Follow the [miner quickstart](../miner-reference/README.md#launch-quickstart) to
install both. The recommended async base miner renders through
`await witness.render(source, RenderOptions(lighting="neutral"))`; the witness
owns Pod authentication and routing. It needs no local renderer container,
renderer URL or image identity. Final submission uses
`await witness.submit(image, source)` outside `generate()`.

The standalone renderer setup below is for local previews and execution hosts
that supply a separate `Renderer`. Its synchronous and async interfaces remain
supported.

## Start the pinned renderer

From the repository root:

```sh
docker build --platform linux/amd64 -f render-service-js/Dockerfile.pinned \
  -t 404-renderer:0.4.8-pinned render-service-js
docker image inspect 404-renderer:0.4.8-pinned --format '{{.Id}}'
```

Use the returned `sha256:...` image ID to start the exact local image and identify
it to the client. For a published image, use its immutable registry digest.

```sh
export RENDERER_IMAGE_ID=sha256:REPLACE_WITH_THE_IMAGE_ID
docker run --rm --init --platform linux/amd64 --cpus=2 --memory=2g \
  -p 127.0.0.1:8000:8000 "$RENDERER_IMAGE_ID"
```

Wait for `GET http://127.0.0.1:8000/ping` to return 200 before generating. The
pinned profile uses the official Puppeteer 24.40.0 image at the digest in
[`Dockerfile.pinned`](../render-service-js/Dockerfile.pinned), including its Chrome
for Testing binary and system libraries. Application packages come from the
existing npm lockfile, including Three.js 0.183.2. The existing software-rendering
flags are retained. The legacy deployment Dockerfile and judge are unchanged.

Use the same final image digest and explicit options in generation and validation.
`renderer_id` records the configured image identity; an HTTP endpoint does not
attest that it is running that image. Pinning reduces runtime drift and does not
promise bit-identical images across every host.

## Render from Python

```sh
python -m pip install ./renderer-client
```

```python
import os
from pathlib import Path

from renderer_client import RenderOptions, RendererClient

source = Path("candidate.js").read_text(encoding="utf-8")
options = RenderOptions(lighting="neutral", img_size=1024)

with RendererClient(
    "http://127.0.0.1:8000",
    renderer_id=os.environ["RENDERER_IMAGE_ID"],
) as renderer:
    Path("preview.png").write_bytes(renderer.render(source, options))
```

`render()` returns exact PNG bytes from `POST /render/grid`. The defaults match
the existing grid API: four angles, 518 pixels per view, gray background and
studio lighting. Every option is sent explicitly. These are API defaults, not
new competition output requirements. One theta/phi pair gives a single-view PNG:

```python
options = RenderOptions(thetas=(30,), phis=(0,), lighting="neutral")
```

For the Witness Pod's renderer, use `witness.render(source, options)` through
`WitnessClient`, or await it through `AsyncWitnessClient`. It owns authentication
and routing and needs no renderer identity, URL, transport or separate client.
The explicit `RendererClient` transport and `ArtifactClient` APIs remain supported
for existing hosts.

An optional `api_key` supplies a Bearer header for an authenticated renderer proxy.
The client raises `RenderRejected` for structured 422 responses; `error.details`
contains the original validation failures and metrics. Transport, authentication,
service and malformed-response failures raise `RenderUnavailable`. It never
converts these to a missing miner output or retries automatically. Responses are
bounded to 32 MiB (256 KiB for rejection details).

## Async rendering

```python
from renderer_client import AsyncRendererClient

async def preview(source, options, renderer_id):
    async with AsyncRendererClient(
        "http://127.0.0.1:8000", renderer_id=renderer_id,
    ) as renderer:
        return await renderer.render(source, options)
```

`AsyncRendererClient` accepts the same URL, renderer identity, API key, timeout
and render options as `RendererClient`, with an async HTTPX `transport` when
needed. For a Witness Pod, use `await witness.render(source, options)`.
`AsyncRenderer` is the awaitable render protocol. PNG bytes, structured rejections
and response bounds match the synchronous API, with no automatic retries. Cancellation closes the active
response and propagates as `asyncio.CancelledError`. Use an async context manager
or `await renderer.aclose()` to close the client.

## Replace rendering during replay

Miner code accepts a `Renderer` instead of constructing an HTTP client internally:

```python
from renderer_client import RecordingRenderer, Renderer, ReplayRenderer

def pipeline(renderer: Renderer, source: str, options: RenderOptions) -> bytes:
    return renderer.render(source, options)

with RendererClient(
    "http://127.0.0.1:8000",
    renderer_id=os.environ["RENDERER_IMAGE_ID"],
) as live:
    recording = RecordingRenderer(live)
    original = pipeline(recording, source, options)
    records = recording.records

# This implementation creates no HTTP client and needs no running renderer.
replay = ReplayRenderer(records, renderer_id=os.environ["RENDERER_IMAGE_ID"])
assert pipeline(replay, source, options) == original
replay.assert_consumed()
```

Records bind the renderer identity, exact source hash, all normalized options,
and either PNG bytes or a structured rejection. Replay matches each request to
an unused observation with the same inputs, regardless of the recording or request
order. Identical calls consume separate observations. `ReplayMismatch` reports
changed inputs, extra calls or unconsumed observations. Rejections are replayed
as the same exception data, so a render/refinement loop can follow the same path.
Concurrent renders may finish in any order. Infrastructure failures propagate
without becoming render observations. A recording interrupted by a renderer failure
cannot export `records`, even if a later call succeeds; this prevents a partial
interaction from looking replayable.

The records are in-memory values, not an authenticated transcript format. The
validator must establish image provenance or apply its agreed render-comparison
policy before supplying records. This client does not decide a pixel tolerance,
trust arbitrary images, persist R2 evidence, or replay model calls. `WitnessClient`
remains the separate model client.

## Checks

```sh
python -m pip install -e './renderer-client[dev]'
cd renderer-client
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

For real rendering and offline-replay checks against the locally built image:

```sh
RENDERER_TEST_URL=http://127.0.0.1:8000 \
RENDERER_TEST_ID="$RENDERER_IMAGE_ID" python -m pytest tests/test_integration.py
```

These checks render repository fixtures and make no model calls.

Legacy-only dependency finding: `npm audit --omit=dev` reports eight high-severity
entries in the renderer's unchanged npm lockfile, in Puppeteer's browser/transport
dependency tree. Dependency upgrades remain a separate change; this slice pins
the existing versions.
