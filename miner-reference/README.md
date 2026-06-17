# 404-GEN Miner Reference

Reference bundle for 404-GEN miners: specifications, examples, a local validator, a visual previewer, and a reference service implementation.

**Your job is to build a pipeline that turns prompt images into procedural Three.js code.** Each round, prompts are published and you generate one JavaScript module per prompt — code that constructs the depicted 3D object from primitives. You submit these to a CDN for pairwise judging against other miners. Separately, your solution must be deployable as a Docker image so validators can regenerate your outputs and verify them.

## The task

Your goal is to build an **image-to-procedural-code** pipeline. Given a prompt image showing a 3D object, your system analyzes the image and produces a JavaScript module that reconstructs that object from Three.js primitives.

This is fundamentally different from direct 3D mesh generation. Your output is not a mesh — it is **code** that builds the mesh procedurally. The generated script must construct all geometry from the allowed Three.js API surface (primitive geometries, extrusions, lathes, buffer geometry, etc.), apply procedural materials and textures, and fit the result within the bounding box constraint. No precomputed vertex data, no embedded assets, no external resources.

### Prompt images

Each prompt is a single image (PNG or JPG) showing a 3D object. Your pipeline analyzes the image and produces a Three.js module that reconstructs the depicted object.

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

## Why a batch API for verification

During verification, the orchestrator deploys your Docker image on a 4×H200 pod and sends the same prompts in sequential batches. The batch model gives you full control over how verification runs on your hardware:

- **Scheduling is yours.** How to parallelize across GPUs, when to retry a failed generation, how to allocate compute to harder prompts — all up to you.
- **Hardware diagnostics are yours.** Your code decides when a pod is good enough and when to request a replacement.
- **Partial results count.** If one prompt fails, skip it and return the rest. 31 out of 32 is far better than timing out.
- **Models stay warm.** No container teardown between batches. Load once, generate four times.

The tradeoff is responsibility: you're building health checks and failure recovery into your service. A buggy service that crashes on startup burns through your pod budget. Test locally before deploying.

## Contents

| Path | Purpose |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Compact rules reference for code-generation LLMs — all constraints, allowed APIs, and patterns in one file |
| [`api_specification.md`](api_specification.md) | HTTP contract between orchestrator and miner pod |
| [`output_specifications.md`](output_specifications.md) | What miners must produce — JS module format, constraints, allowed Three.js APIs |
| [`runtime_specifications.md`](runtime_specifications.md) | How submissions are validated and rendered — sandbox, static analysis, rendering pipeline |
| [`examples/car.js`](examples/car.js) | Canonical hand-written `generate.js` — a low-poly car using primitives, PBR, a `DataTexture`, and the `fitToUnitCube` pattern |
| [`examples/material_variants.js`](examples/material_variants.js) | Three meshes side-by-side using `MeshBasicMaterial`, `MeshStandardMaterial`, and `MeshPhysicalMaterial` (with clearcoat) |
| [`examples/custom_buffergeometry.js`](examples/custom_buffergeometry.js) | A square pyramid built from raw `BufferAttribute`s with explicit positions, normals, UVs, and an index buffer — the pattern for non-primitive geometry |
| [`examples/instanced_mesh.js`](examples/instanced_mesh.js) | A double-helix of 240 cubes from a single `InstancedMesh` — per-instance translation and rotation via `setMatrixAt`, for any scene with many repeated elements (forests, particles, brick walls) |
| [`examples/points_cloud.js`](examples/points_cloud.js) | A Fibonacci-sphere `Points` cloud with per-vertex colors, demonstrating `Points` + `PointsMaterial` + custom `BufferGeometry` + the `color` attribute |
| [`examples/line_segments.js`](examples/line_segments.js) | A wireframe icosahedron built with `LineSegments` + `LineBasicMaterial` + `EdgesGeometry`, with a height-gradient via per-vertex colors |
| [`examples/prompts/`](examples/prompts/) | Real prompt images (balloon, pumpkin, SUV, clock) |
| [`examples/generated/`](examples/generated/) | Three.js modules generated from those prompts by a VLM + code-LLM pipeline |
| [`examples/viewer.html`](examples/viewer.html) | Live browser previewer for any `generate.js` with HUD metrics; dropdown lists every fixture in this directory and `validator/test/budget-probes/` |
| [`validator/`](validator/) | Conformance validator package (JS/Node), mirrors the production runtime's static and post-execution checks |
| [`tools/validate.js`](tools/validate.js) | CLI wrapper around the validator package |
| [`validator/test/budget-probes/`](validator/test/budget-probes/) | Complexity-tier fixtures showing how realistic outputs sit relative to the caps |
| [`miner_reference/`](miner_reference/) | Reference pod-side HTTP service (Python/FastAPI) implementing the batch API |
| [`docker/Dockerfile`](docker/Dockerfile) | Reference Dockerfile in the required `docker/` location — see [Docker Image Requirements](api_specification.md#docker-image-requirements) |

## Specifications

All three documents are required reading if you're building a miner.

| Document | Covers |
|---|---|
| [API Specification](api_specification.md) | HTTP endpoints (`/health`, `/status`, `/generate`, `/results`), pod lifecycle, batch flow, pod replacement budget, time budget, scoring basics, [Docker image requirements](api_specification.md#docker-image-requirements) |
| [Output Specification](output_specifications.md) | Function signature, execution constraints, literal budget, allowed and prohibited Three.js APIs, failure semantics with rule codes |
| [Runtime Specification](runtime_specifications.md) | Three.js bundle pinning, static analysis, `isolated-vm` sandbox, double-execute render strategy, camera and lighting setup |

## Example prompts and outputs

The [`examples/prompts/`](examples/prompts/) folder contains real prompt images. [`examples/generated/`](examples/generated/) contains corresponding Three.js modules produced by a VLM + code-LLM pipeline. All generated files pass validation.

| Prompt | Generated | Vertices | Draw calls | Notes |
|--------|-----------|----------|------------|-------|
| [`balloon.png`](examples/prompts/balloon.png) | [`balloon.js`](examples/generated/balloon.js) | 1,231 | 4 | Simple: LatheGeometry profile, TubeGeometry string, MeshPhysicalMaterial clearcoat |
| [`pumpkin.png`](examples/prompts/pumpkin.png) | [`pumpkin.js`](examples/generated/pumpkin.js) | 3,409 | 13 | Organic: LatheGeometry segments with procedural DataTexture for surface detail |
| [`suv.png`](examples/prompts/suv.png) | [`suv.js`](examples/generated/suv.js) | 3,331 | 67 | Complex hard-surface: many BoxGeometry parts, TubeGeometry roof rack, TorusGeometry tires |
| [`clock.png`](examples/prompts/clock.png) | [`clock.js`](examples/generated/clock.js) | 5,957 | 109 | Mixed: TorusGeometry frame, CylinderGeometry hands, procedural face texture |

Open the [viewer](examples/viewer.html) and select any entry from the "generated from prompts" dropdown group to preview the generated Three.js output. To compare against the source prompt, open the corresponding image from [`examples/prompts/`](examples/prompts/) in a separate browser tab — the viewer renders only the generated `.js`, not the prompt image itself. The pairs are meant to calibrate your expectations — this is the kind of procedural approximation the competition produces.

## For miners: iterating on your `generate.js`

The primary workflow is writing a `generate.js` file, validating it locally, previewing it visually, and iterating. The validator runs the same static analysis and post-execution checks as the production runtime, so anything that passes locally passes in production.

### 1. Look at the examples

The `examples/` directory holds **hand-written reference fixtures** that demonstrate every allowed pattern in the Output Spec. (For real prompt-to-output pairs from a VLM pipeline, see the "Example prompts and outputs" section above.) Read whichever ones match what your inference pipeline will emit:

| File | Demonstrates |
|---|---|
| **`car.js`** | Primitive geometries, `MeshStandardMaterial`, a procedural `DataTexture`, a transform hierarchy, and the `fitToUnitCube` bounding-box normalization pattern. The canonical "start here" example. |
| **`material_variants.js`** | The three mesh material types: `MeshBasicMaterial` (unlit), `MeshStandardMaterial` (PBR), `MeshPhysicalMaterial` (clearcoat). |
| **`custom_buffergeometry.js`** | Hand-built `BufferGeometry` with explicit `BufferAttribute`s for position/normal/uv and a `setIndex` index buffer. The pattern for emitting raw vertex data instead of using primitives. |
| **`instanced_mesh.js`** | `InstancedMesh` with 240 instances of a single geometry, each with a unique `Matrix4` transform via `setMatrixAt`. Counts as 1 draw call and 24 vertices in the budget regardless of instance count — the right pattern for any scene with many repeated elements (forests, particles, brick walls, crowds). |
| **`points_cloud.js`** | `Points` + `PointsMaterial` + custom `BufferGeometry` + per-vertex colors via the `color` attribute. |
| **`line_segments.js`** | `LineSegments` + `LineBasicMaterial` + `EdgesGeometry` (a built-in helper for extracting edges from any geometry), with a per-vertex color gradient. |

Together these cover every category in the Output Spec's "Allowed Three.js APIs" section. Every example passes the validator with large margins on every cap. They are also wired into the validator's CI runner with metric annotations, so any future regression in their measured vertex/draw/depth/instance/texture counts is caught automatically.

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

## Building your generation pipeline

The part you need to build is the **generation pipeline**: the system that takes a prompt image and produces a conforming Three.js module. This is the core of your miner.

You do not need to own GPUs or rent a dedicated machine just to start iterating on Subnet 17. For prototyping and inner-loop development, you can use serverless inference or serverless GPU providers; `chutes.ai` is a great first option since it is already within the ecosystem, and other hosted services can also help you move faster. Just keep in mind that your final miner must still be reproducible, Dockerized, and compliant with the subnet's open-source and commercial-use rules.

### Suggested architecture

A typical pipeline has multiple stages:

1. **Image analysis.** A vision-language model (VLM) examines the prompt image and identifies the object's structure: what primitives approximate it, their relative sizes, positions, orientations, and spatial relationships.

2. **Code generation.** A code-capable LLM takes the structural analysis and produces a Three.js `generate(THREE)` function. The LLM must be aware of the allowed API surface (see [Output Specification](output_specifications.md#allowed-threejs-apis)) and the constraints (bounding box, vertex limits, literal budget, determinism). Feed it [`AGENTS.md`](AGENTS.md) as context.

3. **Validation and refinement.** Run the local validator on the generated code. If it fails, feed the error back to the LLM and retry. If it passes but renders poorly, refine. This agentic loop — generate, validate, diagnose, retry — is where much of the competitive advantage lies.

4. **Material and texture application.** Procedural materials (`MeshStandardMaterial`, `MeshPhysicalMaterial`) and programmatically generated `DataTexture` instances can significantly improve visual fidelity. Consider tuning these separately from geometry.

This is one approach. You are free to design any pipeline you want. Smaller models are unlikely to handle this problem well; plan for capable VLMs and code LLMs.

### Key constraints to design around

- **50 KB literal budget** prevents embedding precomputed vertex data. Your code must generate geometry algorithmically — parametric shapes, extrusions, lathes, CSG-style composition, instanced meshes.
- **5-second execution timeout** means heavy computation (hundreds of thousands of vertices from custom `BufferGeometry`) needs to be efficient.
- **Determinism** is mandatory — no `Math.random()`, no `Date`. Two executions of the same script must produce identical output.
- **The code cannot access the prompt image at runtime.** Your model must fully encode the image's content as procedural code at generation time. The `generate(THREE)` function receives only the `THREE` namespace.

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

## Reference verification service

Your Docker image must expose an HTTP service for the verification flow (see "How a round works § Verification") and conform to the [Docker Image Requirements](api_specification.md#docker-image-requirements) — most importantly, the build pipeline expects a **`docker/Dockerfile`** at the repository root. You can write the service in any language. If you want a working starting point, there's a Python/FastAPI implementation in [`miner_reference/`](miner_reference) plus a reference [`docker/Dockerfile`](docker/Dockerfile) that builds it. It implements the four endpoints from the [API Specification](api_specification.md) and demonstrates:

- Pod state machine (`warming_up` → `ready` → `generating` → `complete`)
- GPU health benchmark at startup and per-batch: stress-tests each GPU's compute throughput (TFLOPS) and checks per-GPU VRAM via `torch.cuda`, requests `replace` when any GPU is degraded (if budget allows)
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

To build the Docker image the same way the orchestrator's pipeline does (build context = repo root, Dockerfile at `docker/Dockerfile`):

```bash
docker build -f docker/Dockerfile -t miner-reference .
docker run -p 10006:10006 miner-reference
```

`miner_reference/threejs_placeholder.py` returns the canonical [`examples/car.js`](examples/car.js) module for every prompt — the same fixture the validator treats as known-good. Replace `threejs_placeholder.py` with your inference pipeline when you're ready to generate real per-prompt output.

### Hardware

Each verification pod has **4× H200 SXM** GPUs (141 GB HBM3e each, ~564 GB total VRAM). How you distribute work across GPUs is up to you.

### GPU allocation

Common patterns for allocating verification pod GPUs:

- Dedicate GPUs to different models (e.g., VLM on GPU 0, code LLM on GPUs 1–3).
- Process multiple prompts in parallel across GPUs.
- Use some GPUs for generation and others for refinement/validation loops.

32 prompts per batch means parallelism matters. A serial pipeline that takes 2 minutes per prompt needs ~64 minutes per batch — tight for 4 batches within the 2-hour generation window.

## How a round works

### Generation and submission (your primary workflow)

1. A round opens. The **seed** and **prompt images** are published.
2. You run your pipeline on the prompts — however you want, on your own hardware or any setup.
3. For each prompt, your pipeline produces a `.js` module conforming to the [Output Specification](output_specifications.md).
4. You submit the generated `.js` files to your CDN.

This is the core loop. The quality of the Three.js code you produce here is what determines your score.

### Judging

5. Validators download your submissions, render each `.js` file in a sandboxed environment (static analysis → `isolated-vm` execution → headless Chrome render).
6. Rendered images are compared against the original prompt images via **pairwise VLM duels** against other miners' outputs.
7. **Failed prompts are automatic losses** — a prompt that fails validation or rendering scores zero against every other miner. Reliability first, then quality.

### Verification

Verification confirms one thing: that your Docker image actually produces what you submitted. It runs in two phases.

8. **Regeneration (generation orchestrator).** When you become a winner candidate, validators deploy your Docker image on a 4×H200 pod and send the same prompts via the batch API (see [API Specification](api_specification.md)). Your service regenerates the outputs and validators render them into the same view bundles used for judging.

9. **Verification duel (judge service).** The judge then runs a duel between your **submitted** outputs and your **regenerated** outputs, prompt by prompt, using the same multi-stage VLM pipeline used for tournament duels.

   You **pass** if the duel is a draw or your regeneration wins overall. You **fail** if your submission strictly beats your regeneration — i.e. you submitted something noticeably better than what your published code can actually produce. The margin requirement is 0%: any net loss for `generated` rejects you.

10. **Generation time can also reject you.** Independently of the duel, your submission may be rejected if your pipeline cannot regenerate the prompts within the round's total generation-time budget (see [Hardware](#hardware) — 4 batches in the 2-hour window). The exact policy here is still being finalized: it may be strict (over-budget total time → reject) or lenient (only the batches that fit are accepted, the rest are dropped). Either way, design your pipeline to comfortably meet the per-batch and total-time targets.

The batch API, Docker image, and pod lifecycle described in this bundle are the **verification interface**. You need to implement them so your solution can be verified, but they are not the primary competition surface — the quality of your generated `.js` files is.

## See also

- Pinned Three.js bundle: `three@0.183.2` (r183)
- Validator internals: [`validator/README.md`](validator/README.md)
- Canonical rule codes and the failure table: [`output_specifications.md`](output_specifications.md#failure-semantics)
