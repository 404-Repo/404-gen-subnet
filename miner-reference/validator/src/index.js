/**
 * Public API: validate(source) → result
 *
 * Runs the three pipeline stages:
 *
 *   1. parse           — file size + parse + structural failures (main thread)
 *   2. static_analysis — AST-based rule checks (main thread)
 *   3. worker          — module load + generate() + post-validation
 *                        (Node worker thread with real preemption + heap cap)
 *
 * The worker stage covers what the runtime spec calls `module_load`,
 * `execution`, and `post_validation` — the boundaries between them are
 * reported via the `failures[].stage` field.
 */

import { parseSource } from './parse.js';
import { staticAnalyze } from './staticAnalysis.js';
import { execute } from './execute.js';

export async function validate(source) {
  // Stage 1: parse
  const { ast, failures: parseFailures } = parseSource(source);
  if (parseFailures.length > 0) {
    return {
      passed: false,
      stagesRun: ['parse'],
      failures: parseFailures,
      metrics: null,
      moduleLoadMs: null,
      executionMs: null,
      totalMs: 0,
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
      moduleLoadMs: null,
      executionMs: null,
      totalMs: 0,
    };
  }

  // Stage 3: worker — module load + execution + post-validation
  const workerResult = await execute(source);
  const passed = workerResult.failures.length === 0;

  // stagesRun reflects the furthest stage reached, determined from the worker
  // output. A clean pass means every stage ran.
  let stagesRun;
  if (passed) {
    stagesRun = [
      'parse',
      'static_analysis',
      'module_load',
      'execution',
      'post_validation',
    ];
  } else {
    const deepest = workerResult.failures[workerResult.failures.length - 1].stage;
    const worker_stages = ['module_load', 'execution', 'post_validation'];
    const idx = worker_stages.indexOf(deepest);
    stagesRun = [
      'parse',
      'static_analysis',
      ...worker_stages.slice(0, idx + 1),
    ];
  }

  return {
    passed,
    stagesRun,
    failures: workerResult.failures,
    metrics: workerResult.metrics,
    moduleLoadMs: workerResult.moduleLoadMs,
    executionMs: workerResult.executionMs,
    totalMs: workerResult.totalMs,
  };
}
