// RuleTester cases for slopbrake/no-tautological-test (rule T1).
import { RuleTester } from "eslint";
import tsParser from "@typescript-eslint/parser";
import { test } from "node:test";
import plugin from "../../slopbrake/kit/typescript/eslint-rules/slopbrake.mjs";

const tester = new RuleTester({ languageOptions: { parser: tsParser, ecmaVersion: 2022, sourceType: "module" } });
const header = 'import { expect, test } from "vitest";\nimport { MAX_LEN, ERR_TOO_LONG, add, total, truncate, validate, sortAll, maxOf, mean, adults, count, getInstance, memo, double, parse } from "../src/shop.js";\n';
const wrap = (body) => `${header}test("t", () => {\n${body}\n});\n`;
const flagged = (body, reason) => ({ code: wrap(body), errors: [{ messageId: "tautology", data: { reason } }] });

test("no-tautological-test", () => {
  tester.run("no-tautological-test", plugin.rules["no-tautological-test"], {
    valid: [
      wrap('expect(total([{ price: 10 }, { price: 5 }])).toBe(15);'),
      wrap('expect(validate("x".repeat(300))).toBe(ERR_TOO_LONG);'),
      wrap("const out = sortAll([3, 1, 2]);\nexpect(out).toEqual([...out].sort());"),
      wrap("expect(add(1, 2)).toBe(3);"),
      wrap("// slopbrake: allow-tautology: 280 is the published API limit\nexpect(MAX_LEN).toBe(280);"),
      // Identity and determinism: the code runs twice, so the comparison can fail.
      wrap("expect(getInstance()).toBe(getInstance());"),
      wrap("expect(memo(5)).toBe(memo(5));"),
      wrap("const seed = 3;\nexpect(add(seed, 0)).toBe(add(seed, 0));"),
      // Length preservation is a property of the result, not a restatement.
      wrap("const xs = [1, 2];\nexpect(double(xs).length).toBe(xs.length);"),
      // A second run of the code under test in the expected value is a property of the result.
      wrap("const xs = [3, 1];\nexpect(adults(xs)).toEqual(adults(xs).filter((p) => p.age >= 18));"),
      wrap("const xs = [3, 1];\nexpect(sortAll(xs)).toEqual([...sortAll(xs)].sort());"),
      // A built-in other than Math is an independent oracle (differential test), like json.loads in Python.
      wrap('const s = "[1]";\nexpect(parse(s)).toEqual(JSON.parse(s));'),
    ],
    invalid: [
      flagged("expect(MAX_LEN).toBe(280);", "asserts the value of implementation constant MAX_LEN; that restates its definition"),
      flagged('expect(truncate("a".repeat(300))).toBe("a".repeat(MAX_LEN));', "expected value is derived from implementation constant MAX_LEN"),
      flagged("const items = [{ price: 10 }, { price: 5 }];\nconst expected = items.reduce((s, i) => s + i.price, 0);\nexpect(total(items)).toBe(expected);",
        "expected value is computed with reduce from the test inputs; use a literal or worked example"),
      flagged("const a = 2, b = 3;\nexpect(add(a, b)).toBe(a + b);", "expected value recomputes the result from the same inputs"),
      flagged("expect(true).toBe(true);", "compares a literal with a literal; nothing is under test"),
      flagged("const x = add(1, 2);\nexpect(x).toBe(x);", "compares an expression with itself"),
      flagged("expect(1).toBeTruthy();", "toBeTruthy() on a literal; nothing is under test"),
      flagged("// slopbrake: allow-tautology:\nexpect(MAX_LEN).toBe(280);", "asserts the value of implementation constant MAX_LEN; that restates its definition"),
      // Inline forms: callback parameters and globals such as Math do not "use the result".
      flagged("const items = [{ price: 10 }, { price: 5 }];\nexpect(total(items)).toBe(items.reduce((s, i) => s + i.price, 0));",
        "expected value is computed with reduce from the test inputs; use a literal or worked example"),
      flagged("const a = 2, b = 3;\nexpect(maxOf(a, b)).toBe(Math.max(a, b));",
        "expected value is computed with max from the test inputs; use a literal or worked example"),
      flagged("const xs = [1, 2, 3];\nexpect(mean(xs)).toBe(xs.reduce((s, x) => s + x, 0) / xs.length);",
        "expected value is computed with reduce from the test inputs; use a literal or worked example"),
      flagged("const people = [{ age: 20 }, { age: 3 }];\nexpect(adults(people)).toEqual(people.filter((p) => p.age >= 18));",
        "expected value is computed with filter from the test inputs; use a literal or worked example"),
      flagged("const items = [1, 2];\nexpect(total(items)).toBe(items.map((i) => price(i)).reduce((a, b) => a + b, 0));",
        "expected value is computed with reduce from the test inputs; use a literal or worked example"),
      flagged("const xs = { a: 1 };\nexpect(count(xs)).toBe(Object.keys(xs).length);",
        "expected value is computed with length from the test inputs; use a literal or worked example"),
      flagged("const xs = [1, 2];\nexpect(count(xs)).toBe(xs.length);",
        "expected value is computed with length from the test inputs; use a literal or worked example"),
      // No code under test re-runs, or a zero-argument call compared by value: still a restatement.
      flagged("const items = [10, 5];\nexpect(items.reduce((a, b) => a + b, 0)).toBe(items.reduce((a, b) => a + b, 0));",
        "compares an expression with itself"),
      flagged("expect(getInstance()).toEqual(getInstance());", "compares an expression with itself"),
    ],
  });
});
