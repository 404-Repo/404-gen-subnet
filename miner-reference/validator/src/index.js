/**
 * Public API: validate(source) → result
 *
 * Runs the four pipeline stages in order:
 *   1. parse           — file size + parse + structural failures
 *   2. static_analysis — AST-based rule checks
 *   3. execution       — dynamic import + call generate(THREE)
 *   4. post_validation — bounded walk over the returned root
 *
 * If any earlier stage fails, later stages are skipped. The shape of the
 * result is stable so callers (CLI, CI, web UI) can render it directly.
 */

import { parseSource } from './parse.js';
import { staticAnalyze } from './staticAnalysis.js';
import { execute } from './execute.js';
import { postValidate } from './postValidation.js';

export async function validate(source) {
  // Stage 1: parse
  const { ast, failures: parseFailures } = parseSource(source);
  if (parseFailures.length > 0) {
    return {
      passed: false,
      stagesRun: ['parse'],
      failures: parseFailures,
      metrics: null,
      executionMs: 0,
    };
  }

  // Stage 2: static analysis
  const staticFailures = staticAnalyze(ast);
  if (staticFailures.length > 0) {
    return {
      passed: false,
      stagesRun: ['parse', 'static_analysis'],
      failures: staticFailures,
      metrics: null,
      executionMs: 0,
    };
  }

  // Stage 3: execute
  const { root, elapsedMs, failure: execFailure } = await execute(source);
  if (execFailure) {
    return {
      passed: false,
      stagesRun: ['parse', 'static_analysis', execFailure.stage],
      failures: [execFailure],
      metrics: null,
      executionMs: elapsedMs,
    };
  }

  // Stage 4: post-validation
  const { failures: postFailures, metrics } = postValidate(root);

  return {
    passed: postFailures.length === 0,
    stagesRun: [
      'parse',
      'static_analysis',
      'module_load',
      'execution',
      'post_validation',
    ],
    failures: postFailures,
    metrics,
    executionMs: elapsedMs,
  };
}
