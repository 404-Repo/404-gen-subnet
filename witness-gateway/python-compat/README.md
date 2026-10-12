# Python runtime compatibility probe

This local probe uses the gateway's pinned Miniflare/workerd and Polkadot
dependencies. It has no RunPod, KV, R2, or production namespace bindings.
It is not a deployable gateway: registration, replay checks, and allocation
policy belong to the application.

Run from `witness-gateway` with Node 22.13 or newer:

```sh
node python-compat/build.mjs
node python-compat/test.mjs
npx wrangler deploy --dry-run --config python-compat/wrangler.jsonc
```

The Python entry point builds the existing `btauth/1` payload and imports a
bundled JavaScript module through `workers.import_from_javascript`. The bridge
only initializes the existing statically imported Polkadot WASM, validates
canonical SS58 addresses, and verifies sr25519 signatures. It contains no
request routing, allocation policy, storage, or relay logic.

The tests use real signatures and reject malformed signatures. Python Durable
Object handlers forward binary frames containing every byte value, including
256 KiB frames, and text messages in both directions. They also verify an actual
hibernation by comparing constructor instance IDs, checking retained socket
attachments, and forwarding another binary frame after waking.

The gateway's existing 2025-10-11 compatibility date and runtime work without
an upgrade. This SDK version omits `web_socket` when constructing a Python
`Response`; the probe uses `js.Response.new` for upgrades through the documented
FFI. Local workerd validation does not establish deployed Cloudflare, RunPod,
or OpenAI behavior.

References: [Python Workers](https://developers.cloudflare.com/workers/languages/python/),
[supported packages](https://developers.cloudflare.com/workers/languages/python/packages/),
[JavaScript FFI](https://developers.cloudflare.com/workers/languages/python/ffi/),
[hibernatable WebSockets](https://developers.cloudflare.com/durable-objects/examples/websocket-hibernation-server/).
