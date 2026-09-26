// RuleTester cases for slopbrake/no-tautological-test (rule T1).
import { RuleTester } from "eslint";
import tsParser from "@typescript-eslint/parser";
import { test } from "node:test";
import plugin from "../../slopbrake/kit/typescript/eslint-rules/slopbrake.mjs";

const tester = new RuleTester({ languageOptions: { parser: tsParser, ecmaVersion: 2022, sourceType: "module" } });
const header = 'import { expect, test } from "vitest";\nimport { MAX_LEN, ERR_TOO_LONG, add, total, truncate, validate, sortAll } from "../src/shop.js";\n';
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
    ],
  });
});
