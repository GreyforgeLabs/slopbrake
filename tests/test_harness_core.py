"""`slopbrake-hook --harness claude|codex|opencode|grok`: each harness's payload reaches the same guard and gate."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]
KIT_SETTINGS = (HOME / "slopbrake/kit/common/.claude/settings.json").read_text()


def sh(cmd, cwd, env=None, stdin=None):
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.state = self.base / "state"
        self.marker = self.base / "stop-ran"

    def env(self, **extra):
        env = {**os.environ, "PYTHONPATH": str(HOME), "HOME": str(self.base / "home"),
               "XDG_STATE_HOME": str(self.state), "XDG_CONFIG_HOME": str(self.base / "config"), **extra}
        if "CLAUDE_PROJECT_DIR" not in extra:  # a Claude Code shell running these tests exports it
            env.pop("CLAUDE_PROJECT_DIR", None)
        return env

    def hook(self, args, payload, **env):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return sh([sys.executable, "-m", "slopbrake.hooks", *args], self.base, env=self.env(**env), stdin=stdin)

    def repo(self, name, managed=True, gate="exit 2"):
        root = self.base / name
        (root / "src").mkdir(parents=True)
        sh(["git", "init", "-q", "-b", "main"], root)
        (root / "src/a.py").write_text("x = 1\n")
        if managed:
            (root / ".claude/hooks").mkdir(parents=True)
            (root / ".claude/slopbrake.json").write_text('{"stack": "python", "kit": "slopbrake"}\n')
            stub = root / ".claude/hooks/require-green.sh"
            stub.write_text(f'#!/bin/sh\ncat >> {self.marker}\necho >> {self.marker}\necho "gate stdout"\n'
                            f'echo "red in $CLAUDE_PROJECT_DIR" >&2\n{gate}\n')
            stub.chmod(0o755)
        sh(["git", "add", "-A"], root)
        sh(["git", *GIT_ID, "commit", "-q", "-m", "init"], root)
        (root / "src/a.py").write_text("x = 2\n")  # an uncommitted edit for the session to own
        return root

    def trust(self, repo):
        trusted = self.base / "config/slopbrake/trusted.json"
        trusted.parent.mkdir(parents=True, exist_ok=True)
        known = json.loads(trusted.read_text())["repos"] if trusted.is_file() else []
        trusted.write_text(json.dumps({"repos": [*known, str(repo)]}))

    def touched(self, session):
        path = self.state / "slopbrake/sessions" / f"{session}.json"
        return json.loads(path.read_text()).get("repos", []) if path.is_file() else []


def grok(event, **fields):
    """A grok payload: camelCase keys, grok's snake_case event in hookEventName, Claude's in hook_event_name."""
    snake = {"PreToolUse": "pre_tool_use", "PostToolUse": "post_tool_use", "Stop": "stop"}[event]
    return {"hookEventName": snake, "hook_event_name": event, "sessionId": "g1", "permissionMode": "default",
            "timestamp": "2026-10-05T12:00:00Z", **fields}


class Usage(Scratch):
    def test_harness_is_optional_and_validated_without_exit_2(self):
        self.assertEqual(self.hook(["--harness", "claude", "stop"], "{}").returncode, 0)
        self.assertEqual(self.hook(["--harness=codex", "stop"], "{}").returncode, 0)
        for args in (["--harness", "bogus", "stop"], ["--harness"], ["--harness", "grok"], ["stop", "extra"]):
            result = self.hook(args, "{}")
            self.assertEqual(result.returncode, 1, args)
            self.assertIn("usage:", result.stderr)


class Grok(Scratch):
    def test_camel_case_terminal_command_is_guarded(self):
        shop = self.repo("shop")
        payload = grok("PreToolUse", cwd=str(shop), workspaceRoot=str(shop), toolName="run_terminal_command",
                       toolUseId="t1", toolInput={"command": "git push --force origin main"})
        result = self.hook(["--harness", "grok", "pre-tool-use"], payload)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("destructive-git guard (H5)", result.stderr)
        # Read as a Claude payload it carries no command at all: that is the gap normalize() closes.
        self.assertEqual(self.hook(["pre-tool-use"], payload).returncode, 0)

    def test_terminal_commands_and_edits_record_the_session_and_its_stop_gates(self):
        shop, web = self.repo("shop"), self.repo("web")
        self.trust(shop)
        self.trust(web)
        bash = grok("PreToolUse", cwd=str(self.base), toolName="run_terminal_command", toolUseId="t1",
                    toolInput={"command": f"touch {shop}/src/new.py"})
        self.assertEqual(self.hook(["--harness", "grok", "pre-tool-use"], bash).returncode, 0)
        (shop / "src/new.py").touch()
        self.hook(["--harness", "grok", "post-tool-use"], dict(bash, hook_event_name="PostToolUse"))
        edit = grok("PostToolUse", cwd=str(self.base), toolName="search_replace", toolUseId="t2",
                    toolInput={"file_path": str(web / "src/a.py")})
        self.hook(["--harness", "grok", "post-tool-use"], edit)
        self.assertEqual(self.touched("g1"), sorted([str(shop), str(web)]))

        stop = grok("Stop", cwd=str(self.base), stopHookActive=False, reason="end_turn")
        result = self.hook(["--harness", "grok", "stop"], stop, CLAUDE_PROJECT_DIR=str(shop))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"{shop}:\nred in ", result.stderr)
        # The gate reads Claude's keys: it gets the normalized payload.
        seen = [json.loads(line) for line in self.marker.read_text().splitlines() if line]
        self.assertEqual({p["session_id"] for p in seen}, {"g1"})
        self.assertIs(seen[0]["stop_hook_active"], False)

    def test_the_observe_only_stop_at_session_end_runs_no_gate(self):
        shop = self.repo("shop")
        self.trust(shop)
        self.hook(["--harness", "grok", "post-tool-use"],
                  grok("PostToolUse", cwd=str(shop), toolName="search_replace", toolInput={"file_path": "src/a.py"}))
        self.assertEqual(self.touched("g1"), [str(shop)])
        result = self.hook(["--harness", "grok", "stop"], grok("Stop", cwd=str(shop), reason="session_end"))
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertFalse(self.marker.exists())


    def test_write_and_hashline_edit_record_their_repo(self):
        shop, web, docs = self.repo("shop"), self.repo("web"), self.repo("docs")
        for tool, args in (("write", {"file_path": str(shop / "src/new.py"), "content": "x"}),
                           ("hashline_edit", {"file_path": str(web / "src/a.py"), "edits": []}),
                           ("hashline_edit", {"path": str(docs / "src/a.py"), "edits": []})):
            payload = grok("PostToolUse", cwd=str(self.base), toolName=tool, toolInput=args)
            self.assertEqual(self.hook(["--harness", "grok", "post-tool-use"], payload).returncode, 0, tool)
        self.assertEqual(self.touched("g1"), sorted([str(shop), str(web), str(docs)]))

    def test_an_unhashable_tool_name_is_ignored_quietly(self):
        shop = self.repo("shop")
        result = self.hook(["--harness", "grok", "post-tool-use"],
                           grok("PostToolUse", cwd=str(shop), toolName={"x": 1}, toolInput={"file_path": "src/a.py"}))
        self.assertEqual((result.returncode, result.stderr), (0, ""))


class PreToolUse(Scratch):
    def test_only_shell_commands_are_guarded(self):
        shop = self.repo("shop")
        patch = "*** Begin Patch\n*** Update File: README.md\n+Never run `git push --force` on main\n*** End Patch\n"
        payload = {"session_id": "c1", "cwd": str(shop), "tool_name": "apply_patch", "tool_input": {"command": patch}}
        self.assertEqual(self.hook(["--harness", "codex", "pre-tool-use"], payload).returncode, 0)
        for name in ("Bash", None):
            bash = dict(payload, tool_name=name, tool_input={"command": "git push --force origin main"})
            self.assertEqual(self.hook(["--harness", "codex", "pre-tool-use"], bash).returncode, 2, name)


PATCH = """*** Begin Patch
*** Add File: {add}
+print("new")
*** Update File: {update}
*** Move to: {moved}
@@
-x = 1
+x = 2
*** Delete File: {delete}
*** End Patch
"""


class ApplyPatch(Scratch):
    def test_codex_patch_targets_record_their_managed_repos(self):
        shop, web, docs, other = self.repo("shop"), self.repo("web"), self.repo("docs"), self.repo("other", False)
        patch = PATCH.format(add="src/b.py", update="../web/src/a.py", moved=str(docs / "src/a.py"),
                             delete=str(other / "src/a.py"))
        payload = {"session_id": "c1", "cwd": str(shop), "hook_event_name": "PostToolUse", "tool_name": "apply_patch",
                   "tool_use_id": "p1", "tool_input": {"command": patch}, "tool_response": "ok"}
        self.assertEqual(self.hook(["--harness", "codex", "post-tool-use"], payload).returncode, 0)
        self.assertEqual(self.touched("c1"), sorted([str(shop), str(web), str(docs)]))

    def test_workdir_is_the_base_for_relative_patch_paths(self):
        shop = self.repo("shop")
        self.repo("src")  # a managed repo named like the patch's first directory, next to the cwd: not the target
        payload = {"session_id": "c1", "cwd": str(self.base), "tool_name": "apply_patch",
                   "tool_input": {"command": "*** Begin Patch\n*** Update File: src/a.py\n*** End Patch\n",
                                  "workdir": "shop"}}
        self.hook(["--harness", "codex", "post-tool-use"], payload)
        self.assertEqual(self.touched("c1"), [str(shop)])

    def test_opencode_patch_text_is_read_too(self):
        shop, web = self.repo("shop"), self.repo("web")
        for session, payload in (("o1", {"tool_input": {"patchText": "*** Add File: src/n.py\n+1\n"}}),
                                 ("o2", {"patchText": f"*** Delete File: {web}/src/a.py\n"})):
            self.hook(["--harness", "opencode", "post-tool-use"],
                      {"session_id": session, "cwd": str(shop), "tool_name": "apply_patch", **payload})
        self.assertEqual((self.touched("o1"), self.touched("o2")), ([str(shop)], [str(web)]))

    def test_a_patch_that_names_no_file_records_nothing(self):
        shop = self.repo("shop")
        self.hook(["--harness", "codex", "post-tool-use"], {"session_id": "c1", "cwd": str(shop),
                                                             "tool_name": "apply_patch", "tool_input": {"command": 7}})
        self.assertEqual(self.touched("c1"), [])


class Stop(Scratch):
    def wired(self, repo):
        (repo / ".claude/settings.json").write_text(KIT_SETTINGS)
        (repo / ".claude/hooks/block-dangerous-git.sh").write_text("#!/bin/sh\n")
        (repo / ".claude/hooks/block-dangerous-git.sh").chmod(0o755)

    def edited(self, repo, session):
        self.hook(["post-tool-use"], {"session_id": session, "cwd": str(repo), "tool_name": "Edit",
                                      "tool_input": {"file_path": "src/a.py"}})

    def test_claude_project_dir_counts_only_for_claude(self):
        shop = self.repo("shop")
        self.trust(shop)
        self.wired(shop)
        self.edited(shop, "s1")
        claude = self.hook(["--harness", "claude", "stop"], {"session_id": "s1"}, CLAUDE_PROJECT_DIR=str(shop))
        self.assertEqual((claude.returncode, claude.stdout), (0, ""))
        self.assertFalse(self.marker.exists())  # the project's own Stop hook gates it under Claude Code
        for harness in ("codex", "opencode", "grok"):
            result = self.hook(["--harness", harness, "stop"], {"session_id": "s1", "sessionId": "s1"},
                               CLAUDE_PROJECT_DIR=str(shop))
            self.assertEqual(result.returncode, 2, harness)
            self.assertIn(f"{shop}:\nred in ", result.stderr)
            self.assertEqual(result.stdout, "")  # Codex: Stop stdout is JSON or nothing

    def test_a_green_stop_prints_nothing_on_stdout(self):
        shop = self.repo("shop", gate="exit 0")
        self.trust(shop)
        self.edited(shop, "s1")
        result = self.hook(["--harness", "codex", "stop"], {"session_id": "s1", "stop_hook_active": False})
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertTrue(self.marker.exists())


if __name__ == "__main__":
    unittest.main()


class HookAliasNeverBlocksByAccident(unittest.TestCase):
    def test_the_alias_passes_harness_flags_through(self):
        # Exit 2 means "block" to every harness: a usage error in the alias must never produce it.
        for args in (["hook", "--harness", "codex", "stop"], ["hook", "--harness=codex", "pre-tool-use"], ["hook", "--bogus"]):
            result = subprocess.run([sys.executable, "-m", "slopbrake", *args], input="{}", capture_output=True, text=True,
                                    env=dict(os.environ, PYTHONPATH=str(HOME)), check=False)
            self.assertNotEqual(result.returncode, 2, (args, result.stderr))
