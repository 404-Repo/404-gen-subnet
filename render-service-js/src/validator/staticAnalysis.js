/**
 * Static analysis: walks the AST and applies every rule from the spec.
 *
 * Rule mapping (rule code → spec section):
 *   MISSING_DEFAULT_EXPORT     → § Function Signature
 *   MULTIPLE_TOP_LEVEL_EXPORTS → § Code Constraints
 *   ASYNC_NOT_ALLOWED          → § Function Signature
 *   FORBIDDEN_IDENTIFIER       → § Prohibited APIs
 *   IDENTIFIER_NOT_ALLOWED     → § Allowed Root Identifiers (Runtime Spec)
 *   FORBIDDEN_THREE_API        → § Prohibited Three.js APIs
 *   THREE_AT_TOP_LEVEL         → § Function Signature
 *   COMPUTED_PROPERTY_ACCESS   → § Code Constraints
 *   FORBIDDEN_PROPERTY_ACCESS  → § Code Constraints (.constructor, __proto__, etc.)
 *   LITERAL_BUDGET_EXCEEDED    → § Code Constraints
 */

import _traverse from '@babel/traverse';
import {
  ALLOWED_ROOT_IDENTIFIERS,
  FORBIDDEN_IDENTIFIERS,
  COMPUTED_ACCESS_GATED,
  FORBIDDEN_PROPERTY_NAMES,
} from './identifiers.js';
import { THREE_ALLOWED, THREE_BLOCKED_SUBMEMBERS } from './threeAllowlist.js';

const traverse = _traverse.default || _traverse;

const LITERAL_BUDGET = 50 * 1024;

export function staticAnalyze(ast) {
  const failures = [];

  // Phase 1: structural checks on top-level statements
  let defaultExport = null;
  let topLevelExports = 0;

  for (const stmt of ast.program.body) {
    if (stmt.type === 'ExportDefaultDeclaration') {
      defaultExport = stmt;
      topLevelExports++;
    } else if (
      stmt.type === 'ExportNamedDeclaration' ||
      stmt.type === 'ExportAllDeclaration'
    ) {
      topLevelExports++;
    }
  }

  if (!defaultExport) {
    failures.push({
      stage: 'static_analysis',
      rule: 'MISSING_DEFAULT_EXPORT',
      detail: 'no `export default` found',
    });
    return failures;
  }

  if (topLevelExports > 1) {
    failures.push({
      stage: 'static_analysis',
      rule: 'MULTIPLE_TOP_LEVEL_EXPORTS',
      detail: `found ${topLevelExports} top-level exports, expected exactly 1`,
    });
  }

  const decl = defaultExport.declaration;
  const isFn =
    decl.type === 'FunctionDeclaration' ||
    decl.type === 'FunctionExpression' ||
    decl.type === 'ArrowFunctionExpression';

  if (!isFn) {
    failures.push({
      stage: 'static_analysis',
      rule: 'MISSING_DEFAULT_EXPORT',
      detail: `default export must be a function, got ${decl.type}`,
    });
    return failures;
  }

  if (decl.async) {
    failures.push({
      stage: 'static_analysis',
      rule: 'ASYNC_NOT_ALLOWED',
      detail: 'default export is an async function',
    });
    return failures;
  }

  // Phase 2: AST walk
  let literalBytes = 0;

  traverse(ast, {
    Function(path) {
      if (path.node.async) {
        failures.push({
          stage: 'static_analysis',
          rule: 'ASYNC_NOT_ALLOWED',
          detail: `async function at line ${loc(path.node)}`,
        });
      }
    },

    AwaitExpression(path) {
      // Top-level await — not enclosed in any async function.
      let p = path.parentPath;
      while (p) {
        if (p.isFunction() && p.node.async) return;
        p = p.parentPath;
      }
      failures.push({
        stage: 'static_analysis',
        rule: 'ASYNC_NOT_ALLOWED',
        detail: `top-level await at line ${loc(path.node)}`,
      });
    },

    ForOfStatement(path) {
      if (path.node.await) {
        failures.push({
          stage: 'static_analysis',
          rule: 'ASYNC_NOT_ALLOWED',
          detail: `for await loop at line ${loc(path.node)}`,
        });
      }
    },

    ImportDeclaration(path) {
      failures.push({
        stage: 'static_analysis',
        rule: 'FORBIDDEN_IDENTIFIER',
        detail: `import statement at line ${loc(path.node)}`,
      });
    },

    'ImportExpression|Import'(path) {
      failures.push({
        stage: 'static_analysis',
        rule: 'FORBIDDEN_IDENTIFIER',
        detail: `dynamic import() at line ${loc(path.node)}`,
      });
    },

    MetaProperty(path) {
      if (path.node.meta.name === 'import') {
        failures.push({
          stage: 'static_analysis',
          rule: 'FORBIDDEN_IDENTIFIER',
          detail: `import.meta at line ${loc(path.node)}`,
        });
      }
    },

    Identifier(path) {
      const name = path.node.name;

      // Skip property names in non-computed MemberExpressions
      if (
        path.parent.type === 'MemberExpression' &&
        path.parent.property === path.node &&
        !path.parent.computed
      ) {
        return;
      }

      // Skip object property keys (non-computed)
      if (
        (path.parent.type === 'ObjectProperty' ||
          path.parent.type === 'ObjectMethod') &&
        path.parent.key === path.node &&
        !path.parent.computed
      ) {
        return;
      }

      // Skip class member keys (non-computed)
      if (
        (path.parent.type === 'ClassProperty' ||
          path.parent.type === 'ClassMethod' ||
          path.parent.type === 'ClassPrivateProperty' ||
          path.parent.type === 'ClassPrivateMethod') &&
        path.parent.key === path.node &&
        !path.parent.computed
      ) {
        return;
      }

      // Skip import/export specifier names
      if (
        path.parent.type === 'ImportSpecifier' ||
        path.parent.type === 'ImportDefaultSpecifier' ||
        path.parent.type === 'ImportNamespaceSpecifier' ||
        path.parent.type === 'ExportSpecifier'
      ) {
        return;
      }

      // 1. Forbidden identifier check (strict, name-based — applies to bindings AND references)
      if (FORBIDDEN_IDENTIFIERS.has(name)) {
        failures.push({
          stage: 'static_analysis',
          rule: 'FORBIDDEN_IDENTIFIER',
          detail: `${name} at line ${loc(path.node)}`,
        });
        return;
      }

      // 2. THREE handling
      if (name === 'THREE') {
        if (path.isReferencedIdentifier() && !path.scope.hasBinding('THREE')) {
          failures.push({
            stage: 'static_analysis',
            rule: 'THREE_AT_TOP_LEVEL',
            detail: `THREE referenced outside generate function at line ${loc(path.node)}`,
          });
        }
        return;
      }

      // 3. Allowlist check (only for free references; locals are OK)
      if (path.isReferencedIdentifier()) {
        if (path.scope.hasBinding(name)) return;
        if (!ALLOWED_ROOT_IDENTIFIERS.has(name)) {
          failures.push({
            stage: 'static_analysis',
            rule: 'IDENTIFIER_NOT_ALLOWED',
            detail: `${name} at line ${loc(path.node)}`,
          });
        }
      }
    },

    MemberExpression(path) {
      const obj = path.node.object;

      if (
        !path.node.computed &&
        path.node.property.type === 'Identifier' &&
        FORBIDDEN_PROPERTY_NAMES.has(path.node.property.name)
      ) {
        failures.push({
          stage: 'static_analysis',
          rule: 'FORBIDDEN_PROPERTY_ACCESS',
          detail: `.${path.node.property.name} at line ${loc(path.node)}`,
        });
      }

      // Computed access on gated globals
      if (
        path.node.computed &&
        obj.type === 'Identifier' &&
        COMPUTED_ACCESS_GATED.has(obj.name)
      ) {
        failures.push({
          stage: 'static_analysis',
          rule: 'COMPUTED_PROPERTY_ACCESS',
          detail: `computed access on ${obj.name} at line ${loc(path.node)}`,
        });
      }

      // Math.random — blocked
      if (
        obj.type === 'Identifier' &&
        obj.name === 'Math' &&
        !path.node.computed &&
        path.node.property.type === 'Identifier' &&
        path.node.property.name === 'random'
      ) {
        failures.push({
          stage: 'static_analysis',
          rule: 'FORBIDDEN_IDENTIFIER',
          detail: `Math.random at line ${loc(path.node)}`,
        });
      }

      // THREE.X — check against allowlist (only when THREE is bound, i.e. inside generate)
      if (
        obj.type === 'Identifier' &&
        obj.name === 'THREE' &&
        !path.node.computed &&
        path.node.property.type === 'Identifier' &&
        path.scope.hasBinding('THREE')
      ) {
        const propName = path.node.property.name;
        if (!THREE_ALLOWED.has(propName)) {
          failures.push({
            stage: 'static_analysis',
            rule: 'FORBIDDEN_THREE_API',
            detail: `THREE.${propName} at line ${loc(path.node)}`,
          });
        }
      }

      // THREE.MathUtils.seededRandom / generateUUID
      if (
        obj.type === 'MemberExpression' &&
        obj.object.type === 'Identifier' &&
        obj.object.name === 'THREE' &&
        !obj.computed &&
        obj.property.type === 'Identifier' &&
        THREE_BLOCKED_SUBMEMBERS[obj.property.name] &&
        !path.node.computed &&
        path.node.property.type === 'Identifier' &&
        THREE_BLOCKED_SUBMEMBERS[obj.property.name].has(path.node.property.name)
      ) {
        failures.push({
          stage: 'static_analysis',
          rule: 'FORBIDDEN_THREE_API',
          detail: `THREE.${obj.property.name}.${path.node.property.name} at line ${loc(path.node)}`,
        });
      }
    },

    StringLiteral(path) {
      literalBytes += Buffer.byteLength(path.node.value, 'utf8');
    },

    NumericLiteral(path) {
      const raw = path.node.extra && path.node.extra.raw;
      literalBytes += (raw ?? String(path.node.value)).length;
    },

    BigIntLiteral(path) {
      const raw = path.node.extra && path.node.extra.raw;
      literalBytes += (raw ?? String(path.node.value)).length;
    },

    TemplateElement(path) {
      const cooked = path.node.value.cooked ?? path.node.value.raw ?? '';
      literalBytes += Buffer.byteLength(cooked, 'utf8');
    },
  });

  if (literalBytes > LITERAL_BUDGET) {
    failures.push({
      stage: 'static_analysis',
      rule: 'LITERAL_BUDGET_EXCEEDED',
      detail: `${literalBytes} bytes (limit ${LITERAL_BUDGET})`,
    });
  }

  return failures;
}

function loc(node) {
  return node.loc?.start?.line ?? '?';
}
