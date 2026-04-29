/**
 * Static analysis: walks the AST and applies every rule from the spec.
 *
 * Rule mapping (rule code → spec section):
 *   MISSING_DEFAULT_EXPORT     → § Function Signature
 *   MULTIPLE_TOP_LEVEL_EXPORTS → § Code Constraints
 *   ASYNC_NOT_ALLOWED          → § Function Signature
 *   FORBIDDEN_IDENTIFIER       → § Prohibited APIs
 *   IDENTIFIER_NOT_ALLOWED     → § Allowed Root Identifiers (Runtime Spec)
 *   FORBIDDEN_THREE_API        → § Prohibited Three.js APIs (known real class, disallowed)
 *   UNKNOWN_THREE_API          → § Prohibited Three.js APIs (name not a real THREE member)
 *   THREE_AT_TOP_LEVEL         → § Function Signature
 *   THREE_ALIAS_FORBIDDEN      → § Prohibited Three.js APIs (aliasing / spreading / rest / array destructure of THREE)
 *   COMPUTED_PROPERTY_ACCESS   → § Code Constraints
 *   LITERAL_BUDGET_EXCEEDED    → § Code Constraints
 */

import _traverse from '@babel/traverse';
import {
  ALLOWED_ROOT_IDENTIFIERS,
  FORBIDDEN_IDENTIFIERS,
  COMPUTED_ACCESS_GATED,
} from './identifiers.js';
import {
  THREE_ALLOWED,
  THREE_BLOCKED_SUBMEMBERS,
  THREE_DISALLOWED_KNOWN,
} from './threeAllowlist.js';

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
        if (path.isReferencedIdentifier()) {
          if (!path.scope.hasBinding('THREE')) {
            failures.push({
              stage: 'static_analysis',
              rule: 'THREE_AT_TOP_LEVEL',
              detail: `THREE referenced outside generate function at line ${loc(path.node)}`,
            });
            return;
          }
          // THREE is bound (parameter of generate or of a helper named
          // `THREE`). Allow it only in these parents:
          //   THREE.X                  — member access (caught downstream)
          //   const X = THREE          — handled by VariableDeclarator visitor
          //   X = THREE                — handled by AssignmentExpression visitor
          //   foo(THREE), new F(THREE) — handled by Call/NewExpression visitor
          //   ...THREE                 — handled by SpreadElement visitor
          // Anywhere else, THREE is escaping into a value position the
          // analyzer cannot follow (return, array element, object value,
          // template string, ternary, etc.) and must be rejected.
          const p = path.parent;
          const ok =
            (p.type === 'MemberExpression' && p.object === path.node) ||
            (p.type === 'VariableDeclarator' && p.init === path.node) ||
            (p.type === 'AssignmentExpression' && p.right === path.node) ||
            ((p.type === 'CallExpression' || p.type === 'NewExpression') &&
              Array.isArray(p.arguments) &&
              p.arguments.includes(path.node)) ||
            (p.type === 'SpreadElement' && p.argument === path.node);
          if (!ok) {
            failures.push({
              stage: 'static_analysis',
              rule: 'THREE_ALIAS_FORBIDDEN',
              detail: `THREE used in disallowed context (${p.type}) at line ${loc(path.node)}`,
            });
          }
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

    // Destructuring / aliasing / spreading THREE — closes bypass paths that
    // the MemberExpression check misses:
    //
    //   const { ShaderMaterial } = THREE;         // extracts a disallowed member
    //   const { Group: G } = THREE;               // aliased extract (allowlisted, OK)
    //   const { MathUtils: { seededRandom } } = THREE;  // nested blocked submember
    //   const { ...rest } = THREE;                // rest grabs everything
    //   const X = THREE;                          // full alias
    //   const { X } = someObj; ... X = THREE;     // reassignment alias
    //   const obj = { ...THREE };                 // object-spread alias
    //   foo(...THREE);                            // call-spread (nonsense, but reject)

    VariableDeclarator(path) {
      const init = path.node.init;
      if (!init) return;
      if (!isThreeReference(init, path)) return;
      handleAssignFromThree(path.node.id, path, loc(path.node));
    },

    AssignmentExpression(path) {
      if (path.node.operator !== '=') return;
      if (!isThreeReference(path.node.right, path)) return;
      handleAssignFromThree(path.node.left, path, loc(path.node));
    },

    SpreadElement(path) {
      if (!isThreeReference(path.node.argument, path)) return;
      failures.push({
        stage: 'static_analysis',
        rule: 'THREE_ALIAS_FORBIDDEN',
        detail: `spread of THREE at line ${loc(path.node)}`,
      });
    },

    RestElement(path) {
      // Handled via handleAssignFromThree when inside a pattern whose source
      // is THREE; nothing to do here in isolation.
    },

    // Cross-function flow: when THREE is passed as an argument, require the
    // receiving parameter to be exactly `Identifier('THREE')`. Otherwise the
    // binding name inside the callee is something like `x` or
    // `{ ShaderMaterial }`, which would escape our THREE-tracking and make
    // the allowlist bypassable (e.g. `const use = ({ShaderMaterial}) => ...;
    // use(THREE);`). Helpers that genuinely need THREE can accept it under
    // its own name — the canonical `fitToUnitCube(THREE, root)` pattern
    // still works.
    CallExpression(path) {
      checkCallWithThreeArg(path);
    },

    NewExpression(path) {
      checkCallWithThreeArg(path);
    },

    MemberExpression(path) {
      const obj = path.node.object;

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
          const isKnown = THREE_DISALLOWED_KNOWN.has(propName);
          failures.push({
            stage: 'static_analysis',
            rule: isKnown ? 'FORBIDDEN_THREE_API' : 'UNKNOWN_THREE_API',
            detail: isKnown
              ? `THREE.${propName} at line ${loc(path.node)}`
              : `THREE.${propName} is not a recognized Three.js API at line ${loc(path.node)}`,
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

  // ── Destructure / alias helpers ────────────────────────────────────────────

  // Returns true iff `node` resolves to the `THREE` parameter identifier in
  // a scope where THREE is actually bound. This prevents triggering on
  // `const THREE = {};` at top level (caught elsewhere) and lets us scope
  // the extra checks to inside the generate function.
  function isThreeReference(node, path) {
    return (
      node &&
      node.type === 'Identifier' &&
      node.name === 'THREE' &&
      path.scope.hasBinding('THREE')
    );
  }

  // Dispatches on the LHS shape of an assignment/declarator whose RHS is THREE.
  // Every reachable shape must be explicitly handled — unhandled shapes would
  // leak THREE into a binding we can't track (e.g. `this.t = THREE`).
  function handleAssignFromThree(lhs, path, line) {
    if (!lhs) return;

    if (lhs.type === 'Identifier') {
      // const X = THREE;  or  X = THREE;
      failures.push({
        stage: 'static_analysis',
        rule: 'THREE_ALIAS_FORBIDDEN',
        detail: `aliasing THREE as \`${lhs.name}\` at line ${line}`,
      });
      return;
    }

    if (lhs.type === 'ObjectPattern') {
      walkObjectPatternFromThree(lhs, line);
      return;
    }

    if (lhs.type === 'ArrayPattern') {
      // `const [a] = THREE;` — nonsense (THREE isn't iterable) but reject
      // to remove an ambiguous escape hatch.
      failures.push({
        stage: 'static_analysis',
        rule: 'THREE_ALIAS_FORBIDDEN',
        detail: `array-destructuring THREE at line ${line}`,
      });
      return;
    }

    if (lhs.type === 'AssignmentPattern') {
      // `const X = THREE` but inside a default — just recurse on the bound
      // name; the default value side is evaluated independently.
      handleAssignFromThree(lhs.left, path, line);
      return;
    }

    if (lhs.type === 'MemberExpression') {
      // `this.t = THREE`, `obj.t = THREE`, `globalThis.T = THREE`, etc.
      // Once THREE lands on a container member, the analyzer can't follow
      // it (e.g. `new this.t.ShaderMaterial()` dereferences a non-`THREE`
      // identifier), so the allowlist would be bypassed.
      failures.push({
        stage: 'static_analysis',
        rule: 'THREE_ALIAS_FORBIDDEN',
        detail: `assigning THREE to a member expression at line ${line}`,
      });
      return;
    }

    // Defensive fallback: any other LHS shape the analyzer hasn't
    // explicitly approved is rejected. This keeps new syntax (private
    // fields, decorators, etc.) from silently introducing a new bypass.
    failures.push({
      stage: 'static_analysis',
      rule: 'THREE_ALIAS_FORBIDDEN',
      detail: `assigning THREE to ${lhs.type} at line ${line}`,
    });
  }

  // Walks an ObjectPattern whose source is THREE and applies allowlist /
  // known-forbidden / unknown / submember-block logic to each extracted key.
  function walkObjectPatternFromThree(pattern, line) {
    for (const prop of pattern.properties) {
      if (prop.type === 'RestElement') {
        // `const { ...rest } = THREE` — rest picks up *every* member including
        // every disallowed one. Reject unconditionally.
        failures.push({
          stage: 'static_analysis',
          rule: 'THREE_ALIAS_FORBIDDEN',
          detail: `rest-destructure of THREE (...${extractRestName(prop)}) at line ${line}`,
        });
        continue;
      }

      if (prop.type !== 'ObjectProperty') continue;

      if (prop.computed) {
        failures.push({
          stage: 'static_analysis',
          rule: 'COMPUTED_PROPERTY_ACCESS',
          detail: `computed destructure of THREE at line ${line}`,
        });
        continue;
      }

      const keyName =
        prop.key.type === 'Identifier'
          ? prop.key.name
          : prop.key.type === 'StringLiteral'
            ? prop.key.value
            : null;
      if (!keyName) continue;

      if (!THREE_ALLOWED.has(keyName)) {
        const isKnown = THREE_DISALLOWED_KNOWN.has(keyName);
        failures.push({
          stage: 'static_analysis',
          rule: isKnown ? 'FORBIDDEN_THREE_API' : 'UNKNOWN_THREE_API',
          detail: isKnown
            ? `THREE.${keyName} (destructured) at line ${line}`
            : `THREE.${keyName} is not a recognized Three.js API (destructured) at line ${line}`,
        });
        continue;
      }

      // Key is allowlisted. If the value side is itself a nested pattern and
      // this key has blocked submembers (e.g. MathUtils.seededRandom), walk it.
      let inner = prop.value;
      if (inner && inner.type === 'AssignmentPattern') inner = inner.left;
      if (
        inner &&
        inner.type === 'ObjectPattern' &&
        THREE_BLOCKED_SUBMEMBERS[keyName]
      ) {
        for (const sub of inner.properties) {
          if (sub.type === 'RestElement') {
            failures.push({
              stage: 'static_analysis',
              rule: 'FORBIDDEN_THREE_API',
              detail: `THREE.${keyName}.{...} rest-destructure at line ${line}`,
            });
            continue;
          }
          if (sub.type !== 'ObjectProperty' || sub.computed) continue;
          const subKey =
            sub.key.type === 'Identifier'
              ? sub.key.name
              : sub.key.type === 'StringLiteral'
                ? sub.key.value
                : null;
          if (subKey && THREE_BLOCKED_SUBMEMBERS[keyName].has(subKey)) {
            failures.push({
              stage: 'static_analysis',
              rule: 'FORBIDDEN_THREE_API',
              detail: `THREE.${keyName}.${subKey} (destructured) at line ${line}`,
            });
          }
        }
      }
    }
  }

  function extractRestName(rest) {
    return rest.argument?.type === 'Identifier' ? rest.argument.name : '?';
  }

  // Inspects a CallExpression or NewExpression and, for each `THREE` argument,
  // enforces the parameter-shape rule on the resolved callee.
  function checkCallWithThreeArg(path) {
    const node = path.node;
    // Fast-path: only act if an argument is literally `THREE` and THREE is
    // bound in the current scope (i.e. we're inside generate or a helper
    // that already received THREE).
    if (!path.scope.hasBinding('THREE')) return;
    if (!Array.isArray(node.arguments)) return;
    let hasThreeArg = false;
    for (const a of node.arguments) {
      if (a && a.type === 'Identifier' && a.name === 'THREE') {
        hasThreeArg = true;
        break;
      }
    }
    if (!hasThreeArg) return;

    const line = loc(node);

    // Special-case: `new THREE.X(...)` — callee is `THREE.Ctor`. Member
    // access is validated by the MemberExpression visitor; we still need to
    // check each THREE argument the same way, but there's no "receiving
    // function" with inspectable params (it's a Three.js class constructor).
    // Treat THREE arguments to THREE-constructors as escape paths: reject.
    if (
      node.type === 'NewExpression' &&
      node.callee.type === 'MemberExpression' &&
      node.callee.object.type === 'Identifier' &&
      node.callee.object.name === 'THREE'
    ) {
      failures.push({
        stage: 'static_analysis',
        rule: 'THREE_ALIAS_FORBIDDEN',
        detail: `passing THREE to \`new THREE.${
          node.callee.property.type === 'Identifier'
            ? node.callee.property.name
            : '?'
        }(...)\` at line ${line}`,
      });
      return;
    }

    const resolved = resolveCallableParams(node.callee, path.scope);
    if (!resolved) {
      failures.push({
        stage: 'static_analysis',
        rule: 'THREE_ALIAS_FORBIDDEN',
        detail: `passing THREE to an unresolvable callee (${node.callee.type}) at line ${line}`,
      });
      return;
    }

    const { params, calleeLabel } = resolved;

    for (let i = 0; i < node.arguments.length; i++) {
      const arg = node.arguments[i];
      if (!arg || arg.type !== 'Identifier' || arg.name !== 'THREE') continue;

      // Locate the matching formal parameter, or determine THREE falls into
      // a rest parameter / beyond the parameter list.
      let param = null;
      let rejectedHere = false;
      for (let p = 0; p < params.length; p++) {
        if (params[p].type === 'RestElement') {
          if (i >= p) {
            failures.push({
              stage: 'static_analysis',
              rule: 'THREE_ALIAS_FORBIDDEN',
              detail: `passing THREE into rest parameter of ${calleeLabel} at line ${line}`,
            });
            rejectedHere = true;
          }
          break;
        }
        if (p === i) {
          param = params[p];
          break;
        }
      }
      if (rejectedHere) continue;
      if (!param) {
        failures.push({
          stage: 'static_analysis',
          rule: 'THREE_ALIAS_FORBIDDEN',
          detail: `passing THREE as extra argument (past formal params) to ${calleeLabel} at line ${line}`,
        });
        continue;
      }

      // Unwrap `= default` wrapping — the binding shape is what matters.
      let shape = param;
      if (shape.type === 'AssignmentPattern') shape = shape.left;

      if (shape.type === 'Identifier') {
        if (shape.name !== 'THREE') {
          failures.push({
            stage: 'static_analysis',
            rule: 'THREE_ALIAS_FORBIDDEN',
            detail: `passing THREE to parameter \`${shape.name}\` of ${calleeLabel} (must be named \`THREE\`) at line ${line}`,
          });
        }
        // else: parameter named THREE → inside the helper, THREE is bound
        // and every existing rule applies as usual.
        continue;
      }

      if (shape.type === 'ObjectPattern') {
        // `const use = ({ ShaderMaterial, Mesh }) => ...; use(THREE)` —
        // run the same extract-member checks that apply to direct
        // `const { ... } = THREE`.
        walkObjectPatternFromThree(shape, line);
        continue;
      }

      if (shape.type === 'ArrayPattern') {
        failures.push({
          stage: 'static_analysis',
          rule: 'THREE_ALIAS_FORBIDDEN',
          detail: `passing THREE to array-pattern parameter of ${calleeLabel} at line ${line}`,
        });
        continue;
      }

      failures.push({
        stage: 'static_analysis',
        rule: 'THREE_ALIAS_FORBIDDEN',
        detail: `passing THREE to ${shape.type} parameter of ${calleeLabel} at line ${line}`,
      });
    }
  }

  // Resolve a callee node to its formal parameter list. Handles:
  //   foo                                  (identifier → function/class binding)
  //   (function(...) {})                  (IIFE — direct Function/Arrow expr)
  //   new (class {...})(...)              (IIFE class)
  // Returns null for method calls, dynamic dispatch, etc. — any THREE arg
  // routed through an unresolvable callee is rejected by the caller.
  function resolveCallableParams(callee, scope) {
    if (!callee) return null;

    if (
      callee.type === 'FunctionExpression' ||
      callee.type === 'ArrowFunctionExpression'
    ) {
      return { params: callee.params, calleeLabel: '<inline function>' };
    }
    if (callee.type === 'ClassExpression') {
      const ctor = findConstructor(callee);
      return {
        params: ctor ? ctor.params : [],
        calleeLabel: '<inline class>',
      };
    }

    if (callee.type === 'Identifier') {
      const binding = scope.getBinding(callee.name);
      if (!binding || !binding.path) return null;
      const nodeDef = binding.path.node;
      if (!nodeDef) return null;

      if (
        nodeDef.type === 'FunctionDeclaration' ||
        nodeDef.type === 'FunctionExpression' ||
        nodeDef.type === 'ArrowFunctionExpression'
      ) {
        return { params: nodeDef.params, calleeLabel: callee.name };
      }
      if (nodeDef.type === 'ClassDeclaration') {
        const ctor = findConstructor(nodeDef);
        return {
          params: ctor ? ctor.params : [],
          calleeLabel: callee.name,
        };
      }
      if (nodeDef.type === 'VariableDeclarator' && nodeDef.init) {
        const init = nodeDef.init;
        if (
          init.type === 'FunctionExpression' ||
          init.type === 'ArrowFunctionExpression'
        ) {
          return { params: init.params, calleeLabel: callee.name };
        }
        if (init.type === 'ClassExpression') {
          const ctor = findConstructor(init);
          return {
            params: ctor ? ctor.params : [],
            calleeLabel: callee.name,
          };
        }
      }
      return null;
    }

    return null;
  }

  function findConstructor(classNode) {
    const body = classNode.body && classNode.body.body;
    if (!body) return null;
    for (const m of body) {
      if (m.type === 'ClassMethod' && m.kind === 'constructor') return m;
    }
    return null;
  }
}

function loc(node) {
  return node.loc?.start?.line ?? '?';
}
