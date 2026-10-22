## Competition 3: Closed-Source Model Competition

This competition focuses on procedurally generating 3D models from image prompts
using approved closed-source OpenAI models. Your pipeline should output Three.js
code that defines geometry, materials and procedural textures, closely matching
the object in the reference image.

### What we provide:

1. 2D image prompts and a published round seed.
2. A fixed Python 3.13 runtime with pinned packages, including NumPy and Pillow.
3. Witness and renderer clients for model requests and candidate previews.
4. Three.js validation, rendering tools and evaluation code for judging outputs.

### Generator Recommendation:

1. Build a pipeline that analyzes the image and decomposes the object into
   procedural geometry, materials and textures.
2. Render candidates, inspect the previews and refine your results within the
   prompt's budget. Use the supplied seed for reproducible local processing.

### Requirements

Keep the following baseline expectations in mind:

1. Submit a Python application with all supporting source and data committed.
   No miner Docker image or GPU declaration is required.
2. Submit `repo`, `commit` and `cdn_url` through your chain commitment. Fix the
   full 40-character commit before prompts and seed are published. Register a
   Finney netuid 17 hotkey with the required 100-alpha lock.
3. Produce self-contained, deterministic Three.js modules using allowlisted APIs.
   Imports, external assets and randomness in generated JavaScript are forbidden.
4. Plan for a nominal **60 OpenAI calls and $5-equivalent Standard token usage per
   prompt**, shared across generation, refinement and discarded attempts. Cached
   inputs count at the full input rate; unused budget cannot transfer between
   prompts. Use the agreed tariff in the miner guide. Standard, Fast and Ultrafast
   count at Standard rates; Flex is forbidden.
5. Pass replay validation and source audit independently of visual quality judging.
   Replay checks reproducibility and budgets using recorded model responses.
   Quality judging compares rendered models against the prompt and current leader.
   The operator generates later incumbent outputs without miner resubmission.

### Requirements for the Python Application:

1. Keep `main.py` at the repository root with this entry point:

   ```python
   def generate(image: bytes, seed: int, witness, renderer) -> str | None:
       ...
   ```

2. Return final JavaScript for the supplied PNG image, or `None` for generation
   failure. Each module must export one synchronous `generate(THREE)` default
   function and meet the Three.js output limits.
3. Use the supplied clients for model requests and rendering, and the fixed runtime
   packages. Do not install or upgrade dependencies during generation or replay.
4. Upload successful `{stem}.js` outputs to your committed CDN base URL and submit
   the runtime's receipt before cutoff, as specified in the miner guide.

For exact requirements, output limits, budget accounting and round
settings, see the [404-GEN miner guide](https://github.com/404-Repo/404-subnet/blob/ca9693e9b9d14eafacc62a5870f3ea205ae1a32e/miner-reference/README.md).
