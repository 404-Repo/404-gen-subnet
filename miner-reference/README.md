# 404-GEN Miner Reference

Reference bundle for 404-GEN miners: specifications, a canonical example, a local validator, a visual previewer, and a reference pod-side service implementation.

The protocol: **the orchestrator launches your Docker image on a 4×H200 pod, sends prompts in 4 sequential batches of 32, and collects one JavaScript module per prompt.** Each module `export default`s a function that constructs a Three.js scene. Your model's job is to produce that code.

## The task

Your goal is to build an **image-to-procedural-code** pipeline. Given a prompt image showing a 3D object, your system analyzes the image and produces a JavaScript module that reconstructs that object from Three.js primitives.

This is fundamentally different from direct 3D mesh generation. Your output is not a mesh — it is **code** that builds the mesh procedurally. The generated script must construct all geometry from the allowed Three.js API surface (primitive geometries, extrusions, lathes, buffer geometry, etc.), apply procedural materials and textures, and fit the result within the bounding box constraint. No precomputed vertex data, no embedded assets, no external resources.

### Prompt images

Each prompt is a single image (PNG or JPG) showing a 3D object. Your service downloads the image from the URL provided in the `/generate` batch, analyzes it, and produces a Three.js module that reconstructs the depicted object.

The prompt set may include furniture, vehicles, architectural elements, household objects, and other categories. Some prompts will be well-suited to procedural approaches (geometric objects, hard-surface models); others will be more challenging (organic forms, complex topology).

### Scoring

Each JavaScript module you return is validated and rendered under the canonical camera and lighting setup defined in the [Runtime Specification](runtime_specifications.md). The rendered image is then compared against the original prompt image by a **VLM (Vision-Language Model) judge** — the same evaluation pipeline used in previous 404 competitions.

Key scoring facts:

- **Quality matters, speed doesn't.** Finishing in 30 minutes scores the same as 119 minutes. Optimize for output quality within the time budget.
- **Failed prompts are automatic losses.** A prompt that fails validation or rendering scores zero against every other miner. Reliability first, then quality.
- **Partial batches always count.** 31 out of 32 is far better than 0 out of 32.
- **Geometric correctness and visual fidelity both matter.** The VLM judge evaluates how well your rendered output matches the prompt image overall.

### Model requirements

- All models in your pipeline must be **open-source with commercial-use-compatible licenses**.
- No closed-source API calls (OpenAI, Anthropic, etc.) during generation.
- Fine-tuned models are allowed if the base model's license permits it.
- Multiple models in a single pipeline are allowed and expected.

## Why batch-based generation

Previous versions used a per-prompt HTTP model where the orchestrator managed retries, pod health checks, and degraded-GPU detection on your behalf. This was expensive to operate and opaque to miners — you had no visibility into scheduling decisions or failure recovery, and no way to optimize for your own hardware.

The batch model flips ownership. The orchestrator sends prompts and collects results; everything in between is yours. You decide how to parallelize across GPUs, when to retry a failed generation, how to detect hardware degradation, and whether to request a pod replacement or push through. This matters because:

- **Scheduling is a competitive lever.** Running two models in parallel, retrying with different parameters, allocating more compute to harder prompts — all legal, all up to you.
- **Hardware diagnostics are a competitive lever.** Your code decides when a pod is good enough and when to burn a replacement. Better diagnostics means fewer wasted pods, more completed batches, and a higher score.
- **No more invisible retries.** In the old system, retries and pod issues were handled behind the scenes. Now everything is transparent — you see your hardware, you make the calls.
- **Partial results always count.** If you can't generate one prompt, skip it and return the rest. 31 out of 32 beats timing out on the whole batch.
- **Models stay warm.** No container teardown between batches. Load once, generate four times.

The tradeoff is more responsibility: you're building health checks, scheduling logic, and failure recovery into your solution. A buggy service that crashes on startup burns through your pod budget fast. Test locally before deploying.

## Contents

| Path | Purpose |
|---|---|
| [`api_specification.md`](api_specification.md) | HTTP contract between orchestrator and miner pod |
| [`output_specifications.md`](output_specifications.md) | What miners must produce — JS module format, constraints, allowed Three.js APIs |
| [`runtime_specifications.md`](runtime_specifications.md) | How submissions are validated and rendered — sandbox, static analysis, rendering pipeline |
| [`examples/car.js`](examples/car.js) | Canonical hand-written `generate.js` — a low-poly car that passes every check |
| [`examples/viewer.html`](examples/viewer.html) | Live browser previewer for any `generate.js` with HUD metrics |
| [`validator/`](validator/) | Conformance validator package (JS/Node), mirrors the production runtime's static and post-execution checks |
| [`tools/validate.js`](tools/validate.js) | CLI wrapper around the validator package |
| [`validator/test/budget-probes/`](validator/test/budget-probes/) | Complexity-tier fixtures showing how realistic outputs sit relative to the caps |
| [`miner_reference/`](miner_reference/) | Reference pod-side HTTP service (Python/FastAPI) implementing the batch API |

## Specifications

All three documents are required reading if you're building a miner.

| Document | Covers |
|---|---|
| [API Specification](api_specification.md) | HTTP endpoints (`/health`, `/status`, `/generate`, `/results`), pod lifecycle, batch flow, pod replacement budget, time budget, scoring basics |
| [Output Specification](output_specifications.md) | Function signature, execution constraints, literal budget, allowed and prohibited Three.js APIs, failure semantics with rule codes |
| [Runtime Specification](runtime_specifications.md) | Three.js bundle pinning, static analysis, `isolated-vm` sandbox, double-execute render strategy, camera and lighting setup |

## For miners: iterating on your `generate.js`

The primary workflow is writing a `generate.js` file, validating it locally, previewing it visually, and iterating. The validator runs the same static analysis and post-execution checks as the production runtime, so anything that passes locally passes in production.

### 1. Look at the canonical example

[`examples/car.js`](examples/car.js) is a hand-written reference that exercises every category in the Output Spec: primitive geometries, PBR materials, a procedural `DataTexture`, a transform hierarchy, and the `fitToUnitCube` pattern for bounding-box normalization. It passes every validator check with large margins on every cap. Read it first.

### 2. Install the validator (one time)

```bash
cd validator
npm install
```

This pulls `@babel/parser`, `@babel/traverse`, and `three@0.183.2` — everything the conformance tool needs.

### 3. Validate your `generate.js` from the command line

```bash
# from the miner-reference root
node tools/validate.js path/to/my-generate.js

# or as JSON for scripts / CI
node tools/validate.js --json path/to/my-generate.js
```

The pretty output shows your stage-by-stage progress, live metrics (vertex count, draw calls, scene depth, instances, texture data, bounding box, execution time), and any failures with stable `{stage, rule, detail}` codes. Exit code is `0` on pass, `1` on fail.

Quick sanity check against the canonical example:

```bash
node tools/validate.js examples/car.js
```

### 4. Preview your output in a browser

The viewer loads any fixture via dynamic `import()`, runs `generate(THREE)`, adds the result to a PBR-lit scene, and shows live metrics in a HUD. It uses the same pinned Three.js bundle as the production renderer, so what you see in the browser matches what the validator will render.

```bash
# from the miner-reference root
python3 -m http.server 8080
```

Then open <http://localhost:8080/examples/viewer.html>. Use the dropdown in the top-left to switch between `car.js` and the budget-probe fixtures. Drag to orbit, scroll to zoom. The wireframe cube shows the `[-0.5, 0.5]` valid region; the RGB axis helper at the origin shows +Z (blue) as the forward axis your model should face.

### 5. Check headroom against realistic outputs

Five hand-written probes in [`validator/test/budget-probes/`](validator/test/budget-probes/) exercise different complexity tiers (a detailed car, a wooden chair, a tree with 2,000 instanced leaves, a Gothic cathedral, a high-poly torus-knot statue). They all validate cleanly and together they tell you how close realistic outputs sit to the current caps.

```bash
cd validator
npm test
```

Output is a summary table: each probe's vertex / draw-call / depth / instance / texture usage, as an absolute number and a percentage of the limit. A realistic output should sit at 0–20% of every cap. If your own `generate.js` is hitting 80% of something, the probes tell you whether that's normal or excessive.

## Reference pod-side service

Every miner needs an HTTP service running inside their Docker image to receive batches from the orchestrator. You can write yours in any language — there's no requirement to use this reference. But if you want a working starting point, there's a Python/FastAPI implementation in [`miner_reference/`](miner_reference). It implements the four endpoints from the API Specification and demonstrates:

- Pod state machine (`warming_up` → `ready` → `generating` → `complete`)
- VRAM-based `replace` requests via `nvidia-smi` (expects ~564 GB total for 4×H200 SXM, requests replacement below that if budget allows)
- Partial-batch failure handling with a `_failed.json` manifest
- Streamed ZIP response from `/results`

```bash
poetry install
python main.py
```

The service starts on port **10006** (the port the orchestrator polls). Run tests with:

```bash
poetry run pytest -v
```

`miner_reference/threejs_placeholder.py` returns the canonical [`examples/car.js`](examples/car.js) module for every prompt — the same fixture the validator treats as known-good. This is end-to-end coherent: the Python service ships exactly what the Node validator will accept. Replace `threejs_placeholder.py` with your inference pipeline when you're ready to generate real per-prompt output.

## Building your generation pipeline

The reference service in [`miner_reference/`](miner_reference/) handles the batch protocol — receiving prompts, managing state, packing results. The part you need to build is the **generation pipeline**: the system that takes a prompt image and produces a conforming Three.js module.

### Suggested architecture

A typical pipeline has multiple stages:

1. **Image analysis.** A vision-language model (VLM) examines the prompt image and identifies the object's structure: what primitives approximate it, their relative sizes, positions, orientations, and spatial relationships.

2. **Code generation.** A code-capable LLM takes the structural analysis and produces a Three.js `generate(THREE)` function. The LLM must be aware of the allowed API surface (see [Output Specification](output_specifications.md#allowed-threejs-apis)) and the constraints (bounding box, vertex limits, literal budget, determinism).

3. **Validation and refinement.** Run the local validator on the generated code. If it fails, feed the error back to the LLM and retry. If it passes but renders poorly, refine. This agentic loop — generate, validate, diagnose, retry — is where much of the competitive advantage lies.

4. **Material and texture application.** Procedural materials (`MeshStandardMaterial`, `MeshPhysicalMaterial`) and programmatically generated `DataTexture` instances can significantly improve visual fidelity. Consider tuning these separately from geometry.

This is one approach. You are free to design any pipeline you want — the orchestrator only cares about the final `.js` files. Smaller models are unlikely to handle this problem well; plan for capable VLMs and code LLMs.

### Key constraints to design around

- **50 KB literal budget** prevents embedding precomputed vertex data. Your code must generate geometry algorithmically — parametric shapes, extrusions, lathes, CSG-style composition, instanced meshes.
- **5-second execution timeout** means heavy computation (hundreds of thousands of vertices from custom `BufferGeometry`) needs to be efficient.
- **Determinism** is mandatory — no `Math.random()`, no `Date`. Two executions of the same script must produce identical output.
- **The code cannot access the prompt image at runtime.** Your model must fully encode the image's content as procedural code at generation time. The `generate(THREE)` function receives only the `THREE` namespace.

### GPU allocation

You have 4× H200 GPUs (141 GB each, ~564 GB total). How you allocate them across pipeline stages is a competitive decision. Common patterns:

- Dedicate GPUs to different models (e.g., VLM on GPU 0, code LLM on GPUs 1–3).
- Process multiple prompts in parallel across GPUs.
- Use some GPUs for generation and others for refinement/validation loops.

32 prompts per batch means parallelism matters. A serial pipeline that takes 2 minutes per prompt needs ~64 minutes per batch — tight for 4 batches within the 2-hour generation window.

### The validator as your inner loop

The local validator runs the same static analysis and post-execution checks as production. Build it into your pipeline as a programmatic check:

```js
import { validate } from '@404-subnet/validator';

const result = await validate(generatedSource);
if (!result.passed) {
  // Feed result.failures back to the LLM for correction
}
```

Code that passes the local validator will pass production. Use the `--json` flag or the library API for machine-readable output that an agent can parse and act on.

## Hardware

Each pod has **4× H200 SXM** GPUs (141 GB HBM3e each, ~564 GB total VRAM). How you distribute work across GPUs is entirely up to you — the orchestrator only cares about the final `.js` files you return.

## How the round works at a glance

1. Orchestrator deploys your Docker image on a 4×H200 pod.
2. It polls `/health` until ready, then polls `/status`.
3. You report `warming_up` while loading models.
4. When `/status` returns `ready`, the orchestrator sends a batch of 32 prompts to `/generate`.
5. You process the batch (in a background task; keep `/status` responsive).
6. When `/status` returns `complete`, the orchestrator downloads a ZIP of `{stem}.js` files from `/results`.
7. You transition back to `ready` for the next batch. Models stay loaded between batches.
8. Repeat for 4 batches (128 total prompts).
9. Each `.js` file is statically analyzed and executed in an `isolated-vm` sandbox, then rendered in a headless Chrome process via a separate double-execute run. The rendered image is compared to the original prompt image by a VLM judge for scoring.
10. **Failed prompts are automatic losses** — there is no partial credit per prompt. Reliability first, then quality.

See the [API Specification](api_specification.md) for the full protocol, pod replacement budget, time budget, and edge cases.

## See also

- Pinned Three.js bundle: `three@0.183.2` (r183)
- Validator internals: [`validator/README.md`](validator/README.md)
- Canonical rule codes and the failure table: [`output_specifications.md`](output_specifications.md#failure-semantics)
