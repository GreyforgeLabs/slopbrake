// The OpenCode adapter (slopbrake/adapters/opencode.js) against a fake client and a fake slopbrake-hook.
// node --test, no dependencies. The fake hook logs each call and exits as the test tells it to.
import { test, beforeEach, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync, chmodSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const PLUGIN = resolve(fileURLToPath(import.meta.url), "../../../slopbrake/adapters/opencode.js");
const base = mkdtempSync(join(tmpdir(), "slopbrake-opencode-"));
const bin = join(base, "bin");
mkdirSync(bin);
writeFileSync(join(bin, "slopbrake-hook"), `#!/bin/sh
event="$3"
payload=$(cat)
printf '%s\\t%s\\t%s\\n' "$*" "\${CLAUDE_PROJECT_DIR-unset}" "$payload" >> "$SB_DIR/log"
[ -f "$SB_DIR/sleep-$event" ] && sleep "$(cat "$SB_DIR/sleep-$event")"
[ -f "$SB_DIR/stderr-$event" ] && cat "$SB_DIR/stderr-$event" >&2
exit "$(cat "$SB_DIR/exit-$event" 2>/dev/null || echo 0)"
`);
chmodSync(join(bin, "slopbrake-hook"), 0o755);
const ORIGINAL_PATH = process.env.PATH;
after(() => rmSync(base, { recursive: true, force: true }));

const plugin = (await import(pathToFileURL(PLUGIN).href)).default;
let dir;
let n = 0;

beforeEach(() => {
  dir = join(base, `case${++n}`);
  mkdirSync(dir);
  process.env.SB_DIR = dir;
  process.env.PATH = `${bin}:${ORIGINAL_PATH}`;
  process.env.CLAUDE_PROJECT_DIR = "/leaked/project";
});

function calls() {
  const log = join(dir, "log");
  return existsSync(log)
    ? readFileSync(log, "utf8").trim().split("\n").map((line) => {
        const [args, projectDir, payload] = line.split("\t");
        return { args, projectDir, payload: JSON.parse(payload) };
      })
    : [];
}

function respond(event, code, stderr = "") {
  writeFileSync(join(dir, `exit-${event}`), String(code));
  writeFileSync(join(dir, `stderr-${event}`), stderr);
}

function fakeClient() {
  const session = {
    prompts: [],
    async promptAsync(options) {
      assert.equal(this, session, "promptAsync must be called as a bound method");
      session.prompts.push(options);
      return {};
    },
  };
  return { session };
}

async function load(client = fakeClient()) {
  return { client, hooks: await plugin.server({ client, directory: "/work/proj" }) };
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function until(condition, ms = 5000) {
  const end = Date.now() + ms;
  while (!condition()) {
    if (Date.now() > end) throw new Error("timed out waiting");
    await sleep(10);
  }
}

const idle = (hooks, sessionID) => hooks.event({ event: { type: "session.idle", properties: { sessionID } } });
const stops = () => calls().filter((c) => c.args.endsWith(" stop"));

test("is a V1 plugin module that does nothing without slopbrake-hook on PATH", async () => {
  assert.equal(typeof plugin.id, "string");
  process.env.PATH = "/nonexistent";
  const { hooks } = await load();
  assert.deepEqual(hooks, {});
});

test("a blocked bash command throws the guard's stderr", async () => {
  respond("pre-tool-use", 2, "BLOCKED by the destructive-git guard (H5): force push\n");
  const { hooks } = await load();
  await assert.rejects(
    hooks["tool.execute.before"]({ tool: "bash", sessionID: "root1", callID: "c1" },
      { args: { command: "git push -f", workdir: "sub" } }),
    { message: /BLOCKED by the destructive-git guard \(H5\): force push/ });
  const [call] = calls();
  assert.equal(call.args, "--harness opencode pre-tool-use");
  assert.equal(call.projectDir, "unset");
  assert.deepEqual(call.payload, {
    hook_event_name: "PreToolUse", session_id: "root1", cwd: "/work/proj/sub", tool_name: "Bash",
    tool_input: { command: "git push -f" }, tool_use_id: "c1",
  });
});

test("an allowed bash command runs, and other tools are not guarded", async () => {
  const { hooks } = await load();
  await hooks["tool.execute.before"]({ tool: "bash", sessionID: "r", callID: "c1" }, { args: { command: "ls" } });
  await hooks["tool.execute.before"]({ tool: "edit", sessionID: "r", callID: "c2" }, { args: { filePath: "a" } });
  assert.deepEqual(calls().map((c) => c.payload.cwd), ["/work/proj"]);
});

test("after-hooks send Claude-shaped payloads for bash, edit, write and apply_patch", async () => {
  const { hooks } = await load();
  const after = (tool, args) => hooks["tool.execute.after"]({ tool, sessionID: "r", callID: `id-${tool}`, args },
    { title: "", output: "", metadata: {} });
  await after("bash", { command: "make", workdir: "/abs" });
  await after("edit", { filePath: "src/a.py", oldString: "x", newString: "y" });
  await after("write", { filePath: "/abs/b.py", content: "z" });
  await after("apply_patch", { patchText: "*** Begin Patch\n*** Add File: c.py\n+x\n*** End Patch" });
  await after("read", { filePath: "src/a.py" });
  const post = { hook_event_name: "PostToolUse", session_id: "r" };
  assert.deepEqual(calls().map((c) => [c.args, c.projectDir, c.payload]), [
    ["--harness opencode post-tool-use", "unset",
      { ...post, cwd: "/abs", tool_name: "Bash", tool_input: { command: "make" }, tool_use_id: "id-bash" }],
    ["--harness opencode post-tool-use", "unset",
      { ...post, cwd: "/work/proj", tool_name: "Edit", tool_input: { file_path: "src/a.py" }, tool_use_id: "id-edit" }],
    ["--harness opencode post-tool-use", "unset",
      { ...post, cwd: "/work/proj", tool_name: "Write", tool_input: { file_path: "/abs/b.py" },
        tool_use_id: "id-write" }],
    ["--harness opencode post-tool-use", "unset",
      { ...post, cwd: "/work/proj", tool_name: "apply_patch",
        tool_input: { patchText: "*** Begin Patch\n*** Add File: c.py\n+x\n*** End Patch" },
        tool_use_id: "id-apply_patch" }],
  ]);
});

test("idle on a red root session prompts with the gate's stderr, at most 3 times in a row", async () => {
  respond("stop", 2, "/r/repo:\nrequire-green: tests failed\n");
  const { client, hooks } = await load();
  await idle(hooks, "root1");
  await until(() => client.session.prompts.length === 1);
  assert.deepEqual(client.session.prompts[0],
    { path: { id: "root1" }, body: { parts: [{ type: "text", text: "/r/repo:\nrequire-green: tests failed" }] } });
  const [first] = stops();
  assert.equal(first.args, "--harness opencode stop");
  assert.equal(first.projectDir, "unset");
  assert.deepEqual(first.payload,
    { hook_event_name: "Stop", session_id: "root1", cwd: "/work/proj", stop_hook_active: false });
  for (const want of [2, 3]) {
    // The continuation itself arrives as a chat message: it must not reset the count.
    await hooks["chat.message"]({ sessionID: "root1" }, { message: {}, parts: [] });
    await idle(hooks, "root1");
    await until(() => client.session.prompts.length === want);
  }
  assert.equal(stops()[1].payload.stop_hook_active, true);
  await hooks["chat.message"]({ sessionID: "root1" }, { message: {}, parts: [] });
  await idle(hooks, "root1");
  await sleep(300);
  assert.equal(client.session.prompts.length, 3);
  // A real user message starts a new run of continuations.
  await hooks["chat.message"]({ sessionID: "root1" }, { message: {}, parts: [] });
  await idle(hooks, "root1");
  await until(() => client.session.prompts.length === 4);
});

test("a green gate does not prompt, and the event handler never waits for the gate", async () => {
  writeFileSync(join(dir, "sleep-stop"), "1");
  const { client, hooks } = await load();
  const started = Date.now();
  await idle(hooks, "root1");
  assert.ok(Date.now() - started < 500, "the event handler returned before the gate finished");
  await until(() => stops().length === 1);
  await sleep(1200);
  assert.equal(client.session.prompts.length, 0);
});

test("a session that got busy while the gate ran is not prompted", async () => {
  respond("stop", 2, "red\n");
  writeFileSync(join(dir, "sleep-stop"), "1");
  const { client, hooks } = await load();
  await idle(hooks, "root1");
  await until(() => stops().length === 1);
  await hooks["chat.message"]({ sessionID: "root1" }, { message: {}, parts: [] });
  await sleep(1500);
  assert.equal(client.session.prompts.length, 0);
});

test("child sessions never run the gate or prompt, and their tools count for the root", async () => {
  respond("stop", 2, "red\n");
  const { client, hooks } = await load();
  await hooks.event({ event: { type: "session.created", properties: { info: { id: "child", parentID: "root1" } } } });
  await hooks.event({ event: { type: "session.updated",
    properties: { info: { id: "grandchild", parentID: "child" } } } });
  await idle(hooks, "child");
  await idle(hooks, "grandchild");
  await hooks["tool.execute.after"]({ tool: "write", sessionID: "grandchild", callID: "w", args: { filePath: "f" } },
    { title: "", output: "", metadata: {} });
  await sleep(300);
  assert.equal(client.session.prompts.length, 0);
  assert.equal(stops().length, 0);
  assert.equal(calls()[0].payload.session_id, "root1");
});
