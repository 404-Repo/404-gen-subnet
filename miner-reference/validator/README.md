# Three.js validator

Use this local tool to check the JavaScript returned by your Python miner.
The [miner guide](../README.md) defines what to submit, and the
[JavaScript reference](../output_specifications.md) lists the output rules and
failure codes.

From `miner-reference/`, with Node.js 20 or newer:

```sh
npm --prefix validator ci
node tools/validate.js examples/car.js
node tools/validate.js --json examples/car.js
```

Exit codes are `0` for passed checks, `1` for validation failures and `2` for
CLI usage/load errors.

## Checks

The tool parses the module, checks exports, identifiers, Three.js APIs and literal
bytes, then runs the module in a Node.js worker thread. It calls the synchronous
default export with the allowlisted Three.js namespace and checks the returned
scene's bounds, vertices, draw calls, depth, instances and textures.

The worker has a five-second timer covering startup, module loading, generation
and post-validation. A stuck worker is terminated. V8's old-generation heap limit
is 256 MB; external `ArrayBuffer` and typed-array storage is outside that limit.
Worker startup and validation overhead count toward the timer, so
`moduleLoadMs + executionMs` can be smaller than the total time.

`src/safeThree.js` restricts namespace access and blocks nondeterministic
`MathUtils` helpers. Static analysis reports disallowed access before execution.
A worker thread is not a security boundary for running arbitrary hostile code.

This tool does not render images, execute the Python miner, verify witnessed
responses, check competition credits, or validate sandbox replay. Passing it
does not establish that the complete submission passes the competition checks.

## Library API

The package exports `validate(source)`:

```js
import { validate } from '@404-subnet/validator';

const result = await validate(source);
console.log(result.passed, result.failures);
```

The result contains:

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
  moduleLoadMs: number | null,
  executionMs: number | null,
  totalMs: number,
}
```

`totalMs` measures the worker stage, including startup and post-validation.
It is zero when parsing or static analysis rejects the source before execution.

## Tests

From `miner-reference/`:

```sh
npm --prefix validator test
```

These tests cover JavaScript resource limits, rejected programs, namespace-access
restrictions and worker startup flags. `test:budget` refers to JavaScript resource
limits, not OpenAI credits. Changes to output rules must keep the analyzer,
runtime namespace restrictions, post-validation checks and JavaScript reference
consistent.
