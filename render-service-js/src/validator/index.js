/**
 * Static-only validation gate for the render service.
 *
 * Runs parse + static analysis from the miner-reference validator.
 * Does NOT run execution or post-validation — that happens in the browser.
 */

import { parseSource } from './parse.js';
import { staticAnalyze } from './staticAnalysis.js';

export function staticValidate(source) {
  const { ast, failures: parseFailures } = parseSource(source);
  if (parseFailures.length > 0) {
    return { passed: false, failures: parseFailures };
  }

  const staticFailures = staticAnalyze(ast);
  if (staticFailures.length > 0) {
    return { passed: false, failures: staticFailures };
  }

  return { passed: true, failures: [] };
}
