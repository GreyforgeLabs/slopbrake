"""Review fixes for `slopbrake user-hooks --harness codex|opencode`: the runnable probe and exact Codex entries."""
import json
import os

from test_harness_install import Scratch


class OldHookOnPath(Scratch):
    """A pre-harness slopbrake-hook answers `pre-tool-use` but rejects `--harness`: every hook would do nothing."""

    def setUp(self):
        super().setUp()
        old = self.base / "old"
        old.mkdir()
        hook = old / "slopbrake-hook"
        hook.write_text('#!/bin/sh\n[ "$1" = pre-tool-use ] && exit 0\necho "usage: slopbrake-hook EVENT" >&2\nexit 1\n')
        hook.chmod(0o755)
        self.old = f"{old}{os.pathsep}{os.environ['PATH']}"

    def test_codex_install_refuses_and_status_is_not_ok(self):
        self.config()
        result = self.gf("install", "--harness", "codex", path=self.old)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--harness codex", result.stderr)
        self.assertFalse((self.codex / "hooks.json").exists())
        self.assertEqual(self.gf("install", "--harness", "codex").returncode, 0)
        result = self.gf("status", "--harness", "codex", "--json", path=self.old)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((json.loads(result.stdout)["runnable"], json.loads(result.stdout)["ok"]), (False, False))

    def test_opencode_install_refuses_and_status_is_not_ok(self):
        plugin = self.home / ".config/opencode/plugins/slopbrake.js"
        result = self.gf("install", "--harness", "opencode", "--json", path=self.old)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--harness opencode", result.stderr)
        self.assertFalse(plugin.exists())
        self.assertEqual(self.gf("install", "--harness", "opencode").returncode, 0)
        result = self.gf("status", "--harness", "opencode", "--json", path=self.old)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((json.loads(result.stdout)["runnable"], json.loads(result.stdout)["ok"]), (False, False))


class CodexExactEntries(Scratch):
    def test_an_entry_under_a_wrong_matcher_is_not_installed_and_install_rewrites_it(self):
        self.config()
        self.assertEqual(self.gf("install", "--harness", "codex").returncode, 0)
        hooks_file = self.codex / "hooks.json"
        data = json.loads(hooks_file.read_text())
        data["hooks"]["PreToolUse"][0]["matcher"] = "^NOPE$"
        data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 5
        hooks_file.write_text(json.dumps(data))
        status = json.loads(self.gf("status", "--harness", "codex", "--json").stdout)
        self.assertEqual(status["events"], {"PreToolUse": False, "PostToolUse": True, "Stop": False})
        self.assertFalse(status["installed"])
        self.assertEqual(self.gf("install", "--harness", "codex").returncode, 0)
        hooks = json.loads(hooks_file.read_text())["hooks"]
        self.assertEqual(hooks["PreToolUse"], [{"matcher": "^Bash$", "hooks": [
            {"type": "command", "command": "slopbrake-hook --harness codex pre-tool-use", "timeout": 30}]}])
        self.assertEqual(hooks["Stop"], [{"hooks": [
            {"type": "command", "command": "slopbrake-hook --harness codex stop", "timeout": 600}]}])
        self.assertTrue(json.loads(self.gf("status", "--harness", "codex", "--json").stdout)["installed"])

    def test_status_warns_that_uninstall_can_renumber_later_entries(self):
        self.config()
        self.assertIn("renumber", self.gf("status", "--harness", "codex").stdout)
