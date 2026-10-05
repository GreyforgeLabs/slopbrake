// The OpenCode adapter records multiedit like edit, so the Stop gate sees files changed only through it.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, rmSync, chmodSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const PLUGIN = resolve(fileURLToPath(import.meta.url), "../../../slopbrake/adapters/opencode.js");
const base = mkdtempSync(join(tmpdir(), "slopbrake-opencode-multiedit-"));
const bin = join(base, "bin");
mkdirSync(bin);
writeFileSync(join(bin, "slopbrake-hook"), `#!/bin/sh\ncat >> "${base}/log"; echo >> "${base}/log"\n`);
chmodSync(join(bin, "slopbrake-hook"), 0o755);
const ORIGINAL_PATH = process.env.PATH;
after(() => {
  process.env.PATH = ORIGINAL_PATH;
  rmSync(base, { recursive: true, force: true });
});

test("multiedit is recorded as an Edit of its filePath", async () => {
  process.env.PATH = `${bin}:${ORIGINAL_PATH}`;
  const plugin = (await import(pathToFileURL(PLUGIN).href)).default;
  const hooks = await plugin.server({ client: { session: {} }, directory: "/work/proj" });
  await hooks["tool.execute.after"]({ tool: "multiedit", sessionID: "r", callID: "m1",
    args: { filePath: "src/a.py", edits: [{ oldString: "x", newString: "y" }] } }, { title: "", output: "", metadata: {} });
  const [payload] = readFileSync(join(base, "log"), "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(payload, { hook_event_name: "PostToolUse", session_id: "r", cwd: "/work/proj", tool_name: "Edit",
    tool_input: { file_path: "src/a.py" }, tool_use_id: "m1" });
});
