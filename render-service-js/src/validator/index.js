/**
 * Static-only validation gate for the render service.
 *
 * Runs parse + static analysis from the miner-reference validator.
 * On success, also returns `transformed`: source with `export default`
 * replaced by `return` using exact AST byte offsets (not regex).
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

  const exportDecl = ast.program.body.find(s => s.type === 'ExportDefaultDeclaration');
  const transformed = source.slice(0, exportDecl.start)
    + 'return '
    + source.slice(exportDecl.declaration.start);

  return { passed: true, failures: [], transformed };
}
