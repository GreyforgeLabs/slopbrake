// RuleTester cases for slopbrake/expect-in-test: a test with no assertion only fails by crashing.
import { RuleTester } from "eslint";
import tsParser from "@typescript-eslint/parser";
import { test } from "node:test";
import plugin from "../../slopbrake/kit/typescript/eslint-rules/slopbrake.mjs";

const tester = new RuleTester({ languageOptions: { parser: tsParser, ecmaVersion: 2022, sourceType: "module" } });
const header = 'import { expect, it, test } from "vitest";\nimport { add } from "../src/shop.js";\n';
const code = (body) => `${header}${body}\n`;
const flagged = (body, count = 1) => ({ code: code(body), errors: Array(count).fill({ messageId: "noAssertion" }) });

test("expect-in-test", () => {
  tester.run("expect-in-test", plugin.rules["expect-in-test"], {
    valid: [
      code('test("adds", () => {\n  expect(add(1, 2)).toBe(3);\n});'),
      code('it("adds", async () => {\n  await expect(Promise.resolve(add(1, 2))).resolves.toBe(3);\n});'),
      code('test("each", () => {\n  [1, 2].forEach((n) => expect(add(n, 0)).toBe(n));\n});'),
      code('test.each([[1, 2, 3]])("adds %i", (a, b, sum) => {\n  expect(add(a, b)).toBe(sum);\n});'),
      code('test("options", { timeout: 50 }, () => {\n  expect.soft(add(1, 2)).toBe(3);\n});'),
      code('import assert from "node:assert/strict";\ntest("node assert", () => {\n  assert.equal(add(1, 2), 3);\n});'),
      code('import nodeAssert from "node:assert";\ntest("aliased", () => {\n  nodeAssert.strictEqual(add(1, 2), 3);\n});'),
      code('import { strictEqual } from "node:assert";\ntest("named", () => {\n  strictEqual(add(1, 2), 3);\n});'),
      code('test("helper", () => {\n  checkSum(1, 2);\n});\nfunction checkSum(a, b) {\n  expect(add(a, b)).toBe(a + b === 3 ? 3 : 0);\n}'),
      code('const assertAdds = (a, b, s) => expect(add(a, b)).toBe(s);\nit("arrow helper", () => {\n  assertAdds(1, 2, 3);\n});'),
      code('test.skip("later", () => {\n  add(1, 2);\n});'),
      code('it.todo("someday");'),
      code('test.todo("someday", () => {});'),
      code('// slopbrake: allow-no-assert: smoke test, crashing is the failure\ntest("smoke", () => {\n  add(1, 2);\n});'),
      code('describe("suite", () => {\n  add(1, 2);\n});'),
      code('const ok = /x/.test("x", () => {});\nconst apply = (f, x) => f(x);\ntest("chain", () => {\n  expect(apply(add, ok)).toBe(true);\n});'),
    ],
    invalid: [
      flagged('test("runs", () => {\n  add(1, 2);\n});'),
      flagged('it("runs", async function () {\n  await add(1, 2);\n});'),
      flagged('test.only("runs", () => {\n  add(1, 2);\n});'),
      flagged('test.each([[1, 2]])("runs %i", (a, b) => {\n  add(a, b);\n});'),
      flagged('describe("suite", () => {\n  it("a", () => add(1, 2));\n  it("b", () => {\n    expect(add(1, 2)).toBe(3);\n  });\n});'),
      flagged('function outer() {\n  checkSum();\n}\nfunction checkSum() {\n  expect(add(1, 2)).toBe(3);\n}\ntest("two levels", () => {\n  outer();\n});'),
      flagged('// slopbrake: allow-no-assert:\ntest("no reason", () => {\n  add(1, 2);\n});'),
    ],
  });
});
