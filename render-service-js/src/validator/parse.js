/**
 * File-size and parse step. Returns the AST or a structured failure.
 */

import { parse as babelParse } from '@babel/parser';

export const MAX_FILE_BYTES = 1024 * 1024;

export function parseSource(source) {
  const bytes = Buffer.byteLength(source, 'utf8');
  if (bytes > MAX_FILE_BYTES) {
    return {
      ast: null,
      failures: [
        {
          stage: 'parse',
          rule: 'FILE_SIZE_EXCEEDED',
          detail: `${bytes} bytes (limit ${MAX_FILE_BYTES})`,
        },
      ],
    };
  }

  let ast;
  try {
    ast = babelParse(source, {
      sourceType: 'module',
      errorRecovery: false,
      attachComment: false,
    });
  } catch (err) {
    return {
      ast: null,
      failures: [
        {
          stage: 'parse',
          rule: 'PARSE_ERROR',
          detail: err.message,
        },
      ],
    };
  }

  return { ast, failures: [] };
}
