// Local ESLint plugin for Slopbrake (rule T1, https://github.com/GreyforgeLabs/slopbrake/blob/main/docs/RULES.md).
//
// slopbrake/no-tautological-test flags equality assertions whose expected value
// restates the code, so the test passes by construction:
//   - a literal compared with a literal (`expect(true).toBe(true)`),
//   - an expression compared with itself, unless it calls the code under test with
//     arguments or is compared by identity (`expect(memo(5)).toBe(memo(5))`,
//     `expect(getInstance()).toBe(getInstance())` re-run the code: determinism and identity checks),
//   - an implementation constant asserted against a literal (`expect(MAX_LEN).toBe(280)`)
//     or an expected value derived from one (`"a".repeat(MAX_LEN)`),
//   - an expected value computed with reduce/map/filter/length/Math.* from the inputs
//     given to the code under test, inline or through a local variable,
//   - an expected value recomputed from the same inputs (`expect(add(a, b)).toBe(a + b)`).
// Property checks built from the result (`expect(xs).toEqual([...xs].sort())`,
// `expect(double(xs).length).toBe(xs.length)`) are fine.
// Suppress one assertion with a reason on the line or the line above:
//   // slopbrake: allow-tautology: <why the expected value is independent>
//
// slopbrake/expect-in-test flags test()/it() callbacks with no assertion: no expect()/assert
// call, directly or in a helper declared at the top of the file (one level). .skip and .todo
// are ignored. Suppress with a reason on the line of the test or the line above:
//   // slopbrake: allow-no-assert: <why crashing is the only failure>

const MATCHERS = new Set(["toBe", "toEqual", "toStrictEqual", "toBeCloseTo"]);
const TRUTHY = new Set(["toBeTruthy", "toBeFalsy", "toBeDefined", "toBeUndefined", "toBeNull"]);
const ASSERT_EQUAL = new Set(["equal", "strictEqual", "deepEqual", "deepStrictEqual"]);
const AGGREGATE_METHODS = new Set(["reduce", "map", "filter", "flatMap", "sort", "toSorted", "max", "min", "sum"]);
const GLOBAL_OBJECTS = new Set(["Math", "Object", "Array", "JSON", "Number", "String", "Boolean", "BigInt", "Date", "Symbol"]);
const TEST_SOURCE = /(^|[/.-])(tests?|fixtures?|helpers?|__tests__|vitest|jest|node:)/;
const ALLOW = /slopbrake:\s*allow-tautology:\s*\S/;
const ALLOW_NO_ASSERT = /slopbrake:\s*allow-no-assert:\s*\S/;
const IDENTITY_MATCHERS = new Set(["toBe", "strictEqual"]);
const TEST_FUNCTIONS = new Set(["test", "it"]);
const ASSERT_NAME = /^(assert|expect|should$)/;
const ASSERT_MODULE = /^(node:)?assert(\/strict)?$|^chai$/;

const isConstName = (name) => /^[A-Z][A-Z0-9_]*$/.test(name) && /[A-Z]/.test(name);

function isLiteral(node) {
  if (!node) return false;
  switch (node.type) {
    case "Literal":
      return true;
    case "TemplateLiteral":
      return node.expressions.length === 0;
    case "UnaryExpression":
      return ["-", "+"].includes(node.operator) && isLiteral(node.argument);
    case "ArrayExpression":
      return node.elements.every((e) => e && isLiteral(e));
    case "ObjectExpression":
      return node.properties.every((p) => p.type === "Property" && !p.computed && isLiteral(p.value));
    case "Identifier":
      return node.name === "undefined";
    default:
      return false;
  }
}

function* walk(node) {
  if (!node || typeof node.type !== "string") return;
  yield node;
  for (const key of Object.keys(node)) {
    if (key === "parent" || key === "loc" || key === "range") continue;
    const value = node[key];
    if (Array.isArray(value)) for (const child of value) yield* walk(child);
    else if (value && typeof value.type === "string") yield* walk(value);
  }
}

function calleeName(call) {
  const callee = call.callee;
  if (callee.type === "Identifier") return callee.name;
  if (callee.type === "MemberExpression" && !callee.computed) return callee.property.name;
  return "";
}

function isGlobalCall(call) {
  const callee = call.callee;
  return callee.type === "MemberExpression" && callee.object.type === "Identifier" && GLOBAL_OBJECTS.has(callee.object.name);
}

function callsUnderTest(node) {
  return [...walk(node)].filter(
    (n) => n.type === "CallExpression" && !AGGREGATE_METHODS.has(calleeName(n)) && !isGlobalCall(n) && !GLOBAL_OBJECTS.has(calleeName(n)),
  );
}

const isFunction = (node) => node && ["ArrowFunctionExpression", "FunctionExpression", "FunctionDeclaration"].includes(node.type);

// Value names read in `node`: not callees, globals such as Math, or callback parameters.
function namesIn(node) {
  const callees = new Set();
  const bound = new Set();
  for (const n of walk(node)) {
    if (n.type === "CallExpression" && n.callee.type === "Identifier") callees.add(n.callee);
    if (isFunction(n)) for (const param of n.params) for (const id of walk(param)) if (id.type === "Identifier") bound.add(id.name);
  }
  const names = new Set();
  for (const n of walk(node)) {
    if (n.type === "Identifier" && !callees.has(n) && !bound.has(n.name) && !GLOBAL_OBJECTS.has(n.name)) names.add(n.name);
  }
  // Drop non-computed property names and object keys: `a.b` uses `a`, not `b`.
  for (const n of walk(node)) {
    if (n.type === "MemberExpression" && !n.computed && n.property.type === "Identifier") names.delete(n.property.name);
    if (n.type === "Property" && !n.computed && n.key.type === "Identifier" && n.key !== n.value) names.delete(n.key.name);
  }
  return names;
}

function aggregateIn(node) {
  for (const n of walk(node)) {
    if (n.type === "CallExpression" && (AGGREGATE_METHODS.has(calleeName(n)) || isGlobalCall(n)) && !n.arguments.every(isLiteral)) {
      return calleeName(n);
    }
    if (n.type === "MemberExpression" && !n.computed && n.property.name === "length") return "length";
  }
  return null;
}

export const noTautologicalTest = {
  meta: {
    type: "problem",
    docs: { description: "Disallow test assertions whose expected value restates the implementation" },
    schema: [],
    messages: { tautology: "Tautological assertion: {{reason}}." },
  },
  create(context) {
    const sourceCode = context.sourceCode ?? context.getSourceCode();
    const constants = new Set();
    const namespaces = new Set();

    const constantName = (node) => {
      if (node.type === "Identifier" && constants.has(node.name)) return node.name;
      if (node.type === "MemberExpression" && !node.computed && node.object.type === "Identifier"
          && namespaces.has(node.object.name) && isConstName(node.property.name)) {
        return `${node.object.name}.${node.property.name}`;
      }
      return null;
    };
    const constantIn = (node) => {
      for (const n of walk(node)) {
        const name = constantName(n);
        if (name) return name;
      }
      return null;
    };
    const constantOperatedOn = (node) => {
      for (const parent of walk(node)) {
        const children = parent.type === "BinaryExpression" ? [parent.left, parent.right]
          : parent.type === "UnaryExpression" ? [parent.argument]
          : parent.type === "MemberExpression" ? [parent.object, ...(parent.computed ? [parent.property] : [])]
          : parent.type === "CallExpression" ? [parent.callee.type === "MemberExpression" ? parent.callee.object : null, ...parent.arguments]
          : [];
        for (const child of children) {
          const name = child && constantName(child);
          if (name) return name;
        }
      }
      return null;
    };

    const resolve = (node) => {
      if (node.type !== "Identifier") return node;
      let scope = sourceCode.getScope ? sourceCode.getScope(node) : context.getScope();
      while (scope) {
        const variable = scope.set.get(node.name);
        if (variable) {
          const def = variable.defs[0];
          if (def && def.node.type === "VariableDeclarator" && def.node.init && def.node.id.type === "Identifier") {
            return def.node.init;
          }
          return node;
        }
        scope = scope.upper;
      }
      return node;
    };

    const judge = (rawActual, rawExpected, identity = false) => {
      if (isLiteral(rawActual) && isLiteral(rawExpected)) return "compares a literal with a literal; nothing is under test";
      if (sourceCode.getText(rawActual) === sourceCode.getText(rawExpected)) {
        const calls = callsUnderTest(rawActual);
        // The code runs twice: an identity or determinism check, not a restatement.
        if (calls.length && (identity || calls.some((c) => c.arguments.length))) return null;
        return "compares an expression with itself";
      }
      let actual = resolve(rawActual);
      let expected = resolve(rawExpected);
      if (callsUnderTest(expected).length && !callsUnderTest(actual).length) [actual, expected] = [expected, actual];
      if (isLiteral(expected)) {
        const name = constantIn(actual);
        return name && !callsUnderTest(actual).length
          ? `asserts the value of implementation constant ${name}; that restates its definition` : null;
      }
      const derived = constantOperatedOn(expected);
      if (derived) return `expected value is derived from implementation constant ${derived}`;
      // `expect(double(xs).length).toBe(xs.length)`: the same measure on both sides is a property.
      const measured = (actual.type === "MemberExpression" && !actual.computed && actual.property.name === "length")
        || (actual.type === "CallExpression" && (AGGREGATE_METHODS.has(calleeName(actual)) || isGlobalCall(actual)));
      if (measured && callsUnderTest(actual).length) return null;
      const inputs = new Set();
      for (const call of callsUnderTest(actual)) for (const arg of call.arguments) for (const n of namesIn(arg)) inputs.add(n);
      const used = namesIn(expected);
      const resultNames = new Set([...namesIn(rawActual), ...namesIn(rawExpected)]);
      const overlap = [...used].some((n) => inputs.has(n));
      const usesResult = [...used].some((n) => resultNames.has(n) && !inputs.has(n));
      const fromInputs = overlap && !usesResult;
      const aggregate = aggregateIn(expected);
      if (aggregate && fromInputs) return `expected value is computed with ${aggregate} from the test inputs; use a literal or worked example`;
      if (fromInputs && [...used].every((n) => inputs.has(n)) && ["BinaryExpression", "TemplateLiteral", "LogicalExpression"].includes(expected.type)) {
        return "expected value recomputes the result from the same inputs";
      }
      return null;
    };

    const allowed = (node) => {
      const line = node.loc.start.line;
      const lines = sourceCode.lines;
      return [line, line - 1].some((l) => l > 0 && l <= lines.length && ALLOW.test(lines[l - 1]));
    };
    const report = (node, reason) => {
      if (reason && !allowed(node)) context.report({ node, messageId: "tautology", data: { reason } });
    };

    return {
      ImportDeclaration(node) {
        if (TEST_SOURCE.test(String(node.source.value))) return;
        for (const spec of node.specifiers) {
          if (spec.type === "ImportNamespaceSpecifier") namespaces.add(spec.local.name);
          else if (isConstName(spec.local.name)) constants.add(spec.local.name);
        }
      },
      CallExpression(node) {
        const callee = node.callee;
        if (callee.type !== "MemberExpression" || callee.computed) return;
        const method = callee.property.name;
        // expect(actual).matcher(expected)
        if (callee.object.type === "CallExpression" && callee.object.callee.type === "Identifier"
            && callee.object.callee.name === "expect" && callee.object.arguments.length) {
          const actual = callee.object.arguments[0];
          if (MATCHERS.has(method) && node.arguments.length) report(node, judge(actual, node.arguments[0], IDENTITY_MATCHERS.has(method)));
          else if (TRUTHY.has(method) && isLiteral(actual)) report(node, `${method}() on a literal; nothing is under test`);
          return;
        }
        // assert.equal(actual, expected)
        if (callee.object.type === "Identifier" && callee.object.name === "assert" && ASSERT_EQUAL.has(method)
            && node.arguments.length >= 2) {
          report(node, judge(node.arguments[0], node.arguments[1], IDENTITY_MATCHERS.has(method)));
        }
      },
    };
  },
};

// Names along a callee chain, root last: `expect(x).not.toBe` -> toBe, not, expect.
// The root is null when the chain does not start at an identifier (`/re/.test(s)`).
function calleeNames(callee) {
  const names = [];
  let n = callee;
  while (n.type === "MemberExpression" || n.type === "CallExpression") {
    if (n.type === "CallExpression") n = n.callee;
    else {
      if (!n.computed && n.property.type === "Identifier") names.push(n.property.name);
      n = n.object;
    }
  }
  return [...names, n.type === "Identifier" ? n.name : null];
}

export const expectInTest = {
  meta: {
    type: "problem",
    docs: { description: "Require an assertion in every test()/it() callback" },
    schema: [],
    messages: { noAssertion: "Test has no assertion: it can only fail by crashing; add an expect() or assert call." },
  },
  create(context) {
    const sourceCode = context.sourceCode ?? context.getSourceCode();
    const assertNames = new Set();
    const tests = [];
    const helpers = new Map();

    const asserts = (fn, helperNames = new Set()) => [...walk(fn.body)].some((n) => {
      if (n.type !== "CallExpression") return false;
      const names = calleeNames(n.callee);
      const root = names[names.length - 1];
      return names.some((name) => ASSERT_NAME.test(name)) || assertNames.has(root)
        || (n.callee.type === "Identifier" && helperNames.has(root));
    });
    const allowed = (node) => {
      const line = node.loc.start.line;
      return [line, line - 1].some((l) => l > 0 && l <= sourceCode.lines.length && ALLOW_NO_ASSERT.test(sourceCode.lines[l - 1]));
    };

    return {
      ImportDeclaration(node) {
        if (ASSERT_MODULE.test(String(node.source.value))) for (const spec of node.specifiers) assertNames.add(spec.local.name);
      },
      "Program > FunctionDeclaration, Program > ExportNamedDeclaration > FunctionDeclaration"(node) {
        if (node.id) helpers.set(node.id.name, node);
      },
      "Program > VariableDeclaration > VariableDeclarator, Program > ExportNamedDeclaration > VariableDeclaration > VariableDeclarator"(node) {
        if (node.id.type === "Identifier" && isFunction(node.init)) helpers.set(node.id.name, node.init);
      },
      CallExpression(node) {
        // test(name, fn), it.only(name, fn), test.each(table)(name, fn); not .skip or .todo.
        const names = calleeNames(node.callee);
        if (!TEST_FUNCTIONS.has(names[names.length - 1]) || names.includes("skip") || names.includes("todo")) return;
        if (names.includes("each") && node.callee.type !== "CallExpression") return;
        const fn = node.arguments.slice(1).find(isFunction);
        if (fn) tests.push({ node, fn });
      },
      "Program:exit"() {
        const asserting = new Set([...helpers].filter(([, fn]) => asserts(fn)).map(([name]) => name));
        for (const { node, fn } of tests) {
          if (!asserts(fn, asserting) && !allowed(node)) context.report({ node, messageId: "noAssertion" });
        }
      },
    };
  },
};

export default { rules: { "no-tautological-test": noTautologicalTest, "expect-in-test": expectInTest } };
