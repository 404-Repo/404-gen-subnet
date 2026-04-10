# miner-reference

Reference miner implementation for the 404-GEN subnet batch generation API.

This project demonstrates how to implement a miner service that conforms to the batch-based generation protocol. It includes:

- **`/health`** — Basic health check (used during pod warmup)
- **`/status`** — Pod status reporting (`warming_up`, `ready`, `generating`, `complete`, `replace`)
- **`/generate`** — Accept a batch of prompt image URLs and start generating
- **`/results`** — Download completed batch results as Three.js JavaScript modules

## Specifications

Three documents define the full protocol. All are required reading:

| Document | Covers |
|----------|--------|
| [API Specification](api_specification.md) | HTTP endpoints, pod lifecycle, batch flow, replacement budget |
| [Output Specification](output_specifications.md) | What miners produce — JavaScript module format, constraints, allowed Three.js APIs |
| [Runtime Specification](runtime_specifications.md) | How submissions are validated and rendered — sandbox, static analysis, rendering pipeline |

## Running

```bash
poetry install
python main.py
```

The service starts on port 10006.

## Testing

```bash
poetry run pytest -v
```

## How it works

The orchestrator launches your Docker image on a 4×H200 pod and sends **128 prompts in 4 sequential batches of 32**. Your service loads models once, processes batches, and returns JavaScript source files that construct Three.js scenes. See the [API Specification](api_specification.md) for the full protocol.

### VRAM check

On startup the service checks total GPU VRAM via `nvidia-smi`. If the pod doesn't have the expected ~564 GB (4×H200 SXM at 141 GB each), it requests a pod replacement via `/status` — but only if replacements remain in the budget.

### Placeholder generation

Instead of running a real model, this reference returns a Three.js JSON scene of a simple car for every prompt. Replace `threejs_placeholder.py` with your actual inference pipeline that produces JavaScript modules conforming to the [Output Specification](output_specifications.md).
