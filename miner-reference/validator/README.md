# @404-subnet/validator

Static and post-execution validator for 404-GEN miner submissions.

This package mirrors the production runtime's static analysis and post-execution checks. Miners can run it locally to iterate on their `generate.js` files before submitting.

## What it checks

| Stage | Rules |
|---|---|
| parse | `FILE_SIZE_EXCEEDED`, `PARSE_ERROR` |
| static_analysis | `MISSING_DEFAULT_EXPORT`, `MULTIPLE_TOP_LEVEL_EXPORTS`, `ASYNC_NOT_ALLOWED`, `FORBIDDEN_IDENTIFIER`, `IDENTIFIER_NOT_ALLOWED`, `FORBIDDEN_THREE_API`, `THREE_AT_TOP_LEVEL`, `COMPUTED_PROPERTY_ACCESS`, `LITERAL_BUDGET_EXCEEDED` |
| module_load / execution | `EXECUTION_THREW`, `INVALID_RETURN_TYPE`, `TIMEOUT_EXCEEDED`, `ASYNC_NOT_ALLOWED` |
| post_validation | `INVALID_RETURN_TYPE`, `EMPTY_SCENE`, `CYCLE_DETECTED`, `VERTEX_LIMIT_EXCEEDED`, `DRAW_CALL_LIMIT_EXCEEDED`, `DEPTH_LIMIT_EXCEEDED`, `INSTANCE_LIMIT_EXCEEDED`, `TEXTURE_DATA_EXCEEDED`, `BOUNDING_BOX_OUT_OF_RANGE` |

Rule codes match the `Rule Code` column in `output_specifications.md` § Failure Semantics.

## What it does NOT check

This package is a **conformance tool**, not a security sandbox.

- **No isolated-vm.** The miner's code runs via dynamic import in the same Node process. The miner is running their own code on their own machine, so no isolation is needed.
- **No heap cap enforcement.** Node has no per-call heap limit. The production validator uses isolated-vm's `memoryLimit`.
- **No render run.** The conformance tool does not run Puppeteer. The post-execution checks happen on the dynamically imported scene root directly.

The static analysis stage is bit-identical to the production validator's. Anything that passes static analysis here will also pass it in production. The execution and post-validation stages match the production *rules* but not the production *isolation* — a malicious miner can do whatever they want on their own machine; the checks here just tell them whether their honest code conforms.

## Install

```bash
cd validator
npm install
```

## Use as a library

```js
import { validate } from '@404-subnet/validator';
import fs from 'node:fs/promises';

const source = await fs.readFile('./my-generate.js', 'utf8');
const result = await validate(source);

if (result.passed) {
  console.log('OK', result.metrics);
} else {
  console.log('FAILED', result.failures);
}
```

## Use via the CLI

See `../tools/validate.js` for the CLI wrapper.

```bash
node ../tools/validate.js ../examples/car.js
```

## Result shape

```ts
{
  passed: boolean,
  stagesRun: string[],
  failures: Array<{ stage: string, rule: string, detail: string }>,
  metrics: {
    vertices: number,
    drawCalls: number,
    maxDepth: number,
    instances: number,
    textureBytes: number,
    bbox: { min: { x, y, z }, max: { x, y, z } } | null,
  } | null,
  executionMs: number,
}
```

## Spec source of truth

The two specifications this validator implements:

- `../output_specifications.md` — what miners must produce
- `../runtime_specifications.md` — how the production validator enforces the rules

When the spec changes, update `src/identifiers.js` and `src/threeAllowlist.js` to match. In a production build, both files should be generated mechanically from the markdown so they cannot drift.
