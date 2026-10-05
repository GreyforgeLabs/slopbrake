// installed by slopbrake (slopbrake user-hooks install --harness opencode); slopbrake overwrites this file.
// The git guard (H5) and the Stop gate (H6) for OpenCode: each tool call and idle root session is handed to
// `slopbrake-hook --harness opencode <event>` as Claude Code shaped hook JSON. Does nothing without it on PATH.
// OpenCode has no Stop hook, so a red gate on session.idle sends its report back as a new prompt.

import { spawn } from "node:child_process";
import { accessSync, constants, statSync } from "node:fs";
import { delimiter, join, resolve } from "node:path";

const PROGRAM = "slopbrake-hook";
const MAX_CONTINUATIONS = 3; // in a row per root session, like rule C2; a real user message resets it
const TIMEOUT_MS = { "pre-tool-use": 30_000, "post-tool-use": 30_000, stop: 600_000 };
const EDITS = { edit: "Edit", multiedit: "Edit", write: "Write" };

function onPath() {
  return (process.env.PATH || "").split(delimiter).some((dir) => {
    try {
      const file = join(dir || ".", PROGRAM);
      accessSync(file, constants.X_OK);
      return statSync(file).isFile();
    } catch {
      return false;
    }
  });
}

// Resolves {code, stderr}; code is null when the hook could not run or timed out (never a block).
function runHook(event, payload) {
  const env = { ...process.env };
  delete env.CLAUDE_PROJECT_DIR; // only Claude Code runs the project's own Stop hook
  return new Promise((done) => {
    let stderr = "";
    let child;
    try {
      child = spawn(PROGRAM, ["--harness", "opencode", event], { env, stdio: ["pipe", "ignore", "pipe"] });
    } catch {
      done({ code: null, stderr });
      return;
    }
    const timer = setTimeout(() => child.kill("SIGKILL"), TIMEOUT_MS[event]);
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.stdin.on("error", () => {});
    child.on("error", () => {
      clearTimeout(timer);
      done({ code: null, stderr });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      done({ code, stderr });
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

export const SlopbrakePlugin = async ({ client, directory }) => {
  if (!onPath()) {
    return {};
  }
  const base = directory || process.cwd();
  const parents = new Map(); // child session -> parent, from session.created/updated info.parentID
  const sessions = new Map(); // root session -> {continuations, running, epoch, own}

  const root = (id) => {
    const seen = new Set();
    while (parents.has(id) && !seen.has(id)) {
      seen.add(id);
      id = parents.get(id);
    }
    return id;
  };
  const state = (id) => {
    if (!sessions.has(id)) {
      sessions.set(id, { continuations: 0, running: false, epoch: 0, own: false });
    }
    return sessions.get(id);
  };

  async function gate(id) {
    const s = state(id);
    if (s.running || s.continuations >= MAX_CONTINUATIONS) {
      return;
    }
    s.running = true;
    const epoch = s.epoch;
    try {
      const { code, stderr } = await runHook("stop", {
        hook_event_name: "Stop", session_id: id, cwd: base, stop_hook_active: s.continuations > 0,
      });
      if (code !== 2 || s.epoch !== epoch) {
        return; // green, could not run, or the session got busy while the gate ran
      }
      s.continuations += 1;
      s.own = true; // the chat.message this prompt causes is not a user message
      try {
        await client.session.promptAsync({
          path: { id }, body: { parts: [{ type: "text", text: stderr.trim() || "slopbrake: the Stop gate is red" }] },
        });
      } catch {
        s.own = false;
      }
    } finally {
      s.running = false;
    }
  }

  return {
    "tool.execute.before": async ({ tool, sessionID, callID }, { args }) => {
      if (tool !== "bash" || typeof args?.command !== "string") {
        return;
      }
      const { code, stderr } = await runHook("pre-tool-use", {
        hook_event_name: "PreToolUse", session_id: root(sessionID), cwd: resolve(base, args.workdir || "."),
        tool_name: "Bash", tool_input: { command: args.command }, tool_use_id: callID,
      });
      if (code === 2) {
        throw new Error(stderr.trim() || "blocked by slopbrake");
      }
    },
    "tool.execute.after": async ({ tool, sessionID, callID, args }) => {
      const payload = { hook_event_name: "PostToolUse", session_id: root(sessionID), cwd: base };
      if (tool === "bash" && typeof args?.command === "string") {
        Object.assign(payload, { cwd: resolve(base, args.workdir || "."), tool_name: "Bash",
          tool_input: { command: args.command } });
      } else if (EDITS[tool] && typeof args?.filePath === "string") {
        Object.assign(payload, { tool_name: EDITS[tool], tool_input: { file_path: args.filePath } });
      } else if (tool === "apply_patch" && typeof args?.patchText === "string") {
        Object.assign(payload, { tool_name: "apply_patch", tool_input: { patchText: args.patchText } });
      } else {
        return;
      }
      payload.tool_use_id = callID;
      await runHook("post-tool-use", payload);
    },
    "chat.message": async ({ sessionID }) => {
      if (!sessionID) {
        return;
      }
      const s = state(sessionID);
      s.epoch += 1;
      if (s.own) {
        s.own = false;
      } else {
        s.continuations = 0;
      }
    },
    event: async ({ event }) => {
      const properties = event?.properties ?? {};
      const info = properties.info;
      if (info?.id && info.parentID) {
        parents.set(info.id, info.parentID);
      }
      const id = typeof properties.sessionID === "string" ? properties.sessionID : undefined;
      if (!id || parents.has(id)) {
        return; // child sessions: the root's gate covers their edits
      }
      const status = properties.status?.type ?? properties.status;
      if (event.type === "session.status" && status && status !== "idle") {
        state(id).epoch += 1;
      } else if (event.type === "session.idle") {
        gate(id).catch(() => {}); // never hold up event processing for the gate
      }
    },
  };
};

// V1 (1.18.29+) calls server(); V2 calls setup() instead, which this plugin does not support yet.
export default {
  id: "slopbrake.opencode",
  server: SlopbrakePlugin,
  setup() {},
};
