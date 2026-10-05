"""`slopbrake user-hooks --harness codex|opencode`: installers that only ever touch temp HOME and settings paths."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
PLUGIN = HOME / "slopbrake/adapters/opencode.js"
OTHER = {"hooks": {"SessionStart": [{"hooks": [{"command": "bash '/x/othertool-state.sh' session", "timeout": 10,
                                                "type": "command"}]}]}}


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.home = self.base / "home"
        self.codex = self.home / ".codex"
        self.codex.mkdir(parents=True)
        bindir = self.base / "bin"
        bindir.mkdir()
        # The slopbrake-hook a harness would run: this checkout, never an installed build.
        hook = bindir / "slopbrake-hook"
        hook.write_text(f'#!/bin/sh\nPYTHONPATH={HOME} exec {sys.executable} -m slopbrake.hooks "$@"\n')
        hook.chmod(0o755)
        self.path = f"{bindir}{os.pathsep}{os.environ['PATH']}"

    def gf(self, *args, path=None):
        env = {k: v for k, v in os.environ.items() if k not in ("CODEX_HOME", "XDG_CONFIG_HOME")}
        env.update(PYTHONPATH=str(HOME), HOME=str(self.home), PATH=path or self.path,
                   XDG_STATE_HOME=str(self.base / "state"))
        return subprocess.run([sys.executable, "-m", "slopbrake", "user-hooks", *args], cwd=self.base, env=env,
                              capture_output=True, text=True, check=False)

    def config(self, text="[features]\nhooks = true\n"):
        (self.codex / "config.toml").write_text(text)


class CodexHooks(Scratch):
    def hooks_json(self):
        return json.loads((self.codex / "hooks.json").read_text())

    def test_install_merges_beside_another_tool_and_uninstall_removes_only_ours(self):
        self.config()
        (self.codex / "hooks.json").write_text(json.dumps(OTHER))
        result = self.gf("install", "--harness", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        hooks = self.hooks_json()["hooks"]
        self.assertEqual(hooks["SessionStart"], OTHER["hooks"]["SessionStart"])
        self.assertEqual(hooks["PreToolUse"], [{"matcher": "^Bash$", "hooks": [
            {"type": "command", "command": "slopbrake-hook --harness codex pre-tool-use", "timeout": 30}]}])
        self.assertEqual(hooks["PostToolUse"], [{"matcher": "^(Bash|apply_patch)$", "hooks": [
            {"type": "command", "command": "slopbrake-hook --harness codex post-tool-use"}]}])
        self.assertEqual(hooks["Stop"], [{"hooks": [
            {"type": "command", "command": "slopbrake-hook --harness codex stop", "timeout": 600}]}])
        before = (self.codex / "hooks.json").read_text()
        self.assertEqual(self.gf("install", "--harness", "codex").returncode, 0)
        self.assertEqual((self.codex / "hooks.json").read_text(), before)  # idempotent: Codex's trust survives
        self.assertEqual(self.gf("uninstall", "--harness", "codex").returncode, 0)
        self.assertEqual(self.hooks_json(), OTHER)
        self.assertEqual((self.codex / "config.toml").read_text(), "[features]\nhooks = true\n")

    def test_install_replaces_a_stale_slopbrake_entry_instead_of_adding_a_second(self):
        self.config()
        stale = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "slopbrake-hook stop"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(stale))
        self.assertEqual(self.gf("install", "--harness", "codex").returncode, 0)
        stops = [h["command"] for e in self.hooks_json()["hooks"]["Stop"] for h in e["hooks"]]
        self.assertEqual(stops, ["slopbrake-hook --harness codex stop"])

    def test_install_refuses_without_the_hooks_feature_and_leaves_config_alone(self):
        for text in (None, "[features]\nhooks = false\n", "model = 'x'\n", "not toml ["):
            with self.subTest(config=text):
                (self.codex / "config.toml").unlink(missing_ok=True)
                if text is not None:
                    self.config(text)
                result = self.gf("install", "--harness", "codex")
                self.assertEqual(result.returncode, 2)
                self.assertIn("hooks = true", result.stderr)
                self.assertFalse((self.codex / "hooks.json").exists())
                if text is not None:
                    self.assertEqual((self.codex / "config.toml").read_text(), text)

    def test_install_refuses_when_slopbrake_hook_cannot_run(self):
        self.config()
        result = self.gf("install", "--harness", "codex", path="/usr/bin:/bin")
        self.assertEqual(result.returncode, 2)
        self.assertIn("slopbrake-hook", result.stderr)
        self.assertFalse((self.codex / "hooks.json").exists())

    def test_status_reports_the_trust_step_and_trusted_entries(self):
        self.config()
        self.assertEqual(self.gf("install", "--harness", "codex").returncode, 0)
        result = self.gf("status", "--harness", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/hooks", result.stdout)
        self.assertIn("not trusted yet: PreToolUse PostToolUse Stop", result.stdout)
        hooks_file = self.codex / "hooks.json"
        self.config("[features]\nhooks = true\n\n[hooks.state]\n\n"
                    f'[hooks.state."{hooks_file}:pre_tool_use:0:0"]\ntrusted_hash = "sha256:x"\n\n'
                    f'[hooks.state."{hooks_file}:stop:0:0"]\ntrusted_hash = "sha256:y"\nenabled = false\n\n'
                    '[hooks.state."plugin@x:hooks/hooks.json:post_tool_use:0:0"]\ntrusted_hash = "sha256:z"\n')
        status = json.loads(self.gf("status", "--harness", "codex", "--json").stdout)
        self.assertEqual(status["trusted"], {"PreToolUse": True, "PostToolUse": False, "Stop": False})
        self.assertTrue(status["features_hooks"])
        self.assertTrue(status["installed"])
        self.assertIn("/hooks", status["note"])

    def test_trust_entry_follows_the_group_index_after_other_tools_entries(self):
        self.config()
        (self.codex / "hooks.json").write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-tool stop"}]}]}}))
        self.assertEqual(self.gf("install", "--harness", "codex").returncode, 0)
        hooks_file = self.codex / "hooks.json"
        self.config("[features]\nhooks = true\n\n"
                    f'[hooks.state."{hooks_file}:stop:0:0"]\ntrusted_hash = "sha256:other"\n')
        self.assertFalse(json.loads(self.gf("status", "--harness", "codex", "--json").stdout)["trusted"]["Stop"])
        self.config("[features]\nhooks = true\n\n"
                    f'[hooks.state."{hooks_file}:stop:1:0"]\ntrusted_hash = "sha256:ours"\n')
        self.assertTrue(json.loads(self.gf("status", "--harness", "codex", "--json").stdout)["trusted"]["Stop"])

    def test_status_is_not_ok_without_the_feature_or_the_entries(self):
        self.config("[features]\nhooks = false\n")
        result = self.gf("status", "--harness", "codex", "--json")
        self.assertEqual(result.returncode, 1)
        status = json.loads(result.stdout)
        self.assertEqual((status["installed"], status["features_hooks"]), (False, False))
        self.assertIn("hooks = true", self.gf("status", "--harness", "codex").stdout)
        self.config("hooks = 1\n\n[features]\nhooks = true\n")  # an odd config is read, not a traceback
        status = json.loads(self.gf("status", "--harness", "codex", "--json").stdout)
        self.assertEqual((status["features_hooks"], status["trusted"]["Stop"]), (True, False))

    def test_settings_overrides_the_hooks_file_and_reads_config_beside_it(self):
        other = self.base / "elsewhere"
        other.mkdir()
        (other / "config.toml").write_text("[features]\nhooks = true\n")
        result = self.gf("install", "--harness", "codex", "--settings", str(other / "hooks.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PreToolUse", json.loads((other / "hooks.json").read_text())["hooks"])
        self.assertFalse((self.codex / "hooks.json").exists())


class OpenCodePlugin(Scratch):
    def setUp(self):
        super().setUp()
        self.plugin = self.home / ".config/opencode/plugins/slopbrake.js"

    def test_install_writes_the_packaged_plugin_and_uninstall_removes_it(self):
        other = self.plugin.parent / "othertool-state.js"
        other.parent.mkdir(parents=True)
        other.write_text("// installed by othertool\n")
        result = self.gf("install", "--harness", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plugin.read_text(), PLUGIN.read_text())
        status = json.loads(self.gf("status", "--harness", "opencode", "--json").stdout)
        self.assertEqual((status["installed"], status["current"], status["ok"]), (True, True, True))
        self.assertEqual(self.gf("uninstall", "--harness", "opencode").returncode, 0)
        self.assertFalse(self.plugin.exists())
        self.assertEqual(other.read_text(), "// installed by othertool\n")
        self.assertIn("not installed", self.gf("status", "--harness", "opencode").stdout)

    def test_install_refreshes_an_older_slopbrake_plugin(self):
        self.plugin.parent.mkdir(parents=True)
        marker = PLUGIN.read_text().splitlines()[0]
        self.plugin.write_text(marker + "\nexport default {};\n")
        status = json.loads(self.gf("status", "--harness", "opencode", "--json").stdout)
        self.assertEqual((status["installed"], status["current"], status["ok"]), (True, False, False))
        self.assertEqual(self.gf("install", "--harness", "opencode").returncode, 0)
        self.assertEqual(self.plugin.read_text(), PLUGIN.read_text())

    def test_install_and_uninstall_leave_a_foreign_file_alone(self):
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_text("export default {id: 'mine'};\n")
        result = self.gf("install", "--harness", "opencode")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not slopbrake's", result.stderr)
        self.gf("uninstall", "--harness", "opencode")
        self.assertEqual(self.plugin.read_text(), "export default {id: 'mine'};\n")

    def test_settings_overrides_the_plugin_path(self):
        target = self.base / "plugins/slopbrake.js"
        self.assertEqual(self.gf("install", "--harness", "opencode", "--settings", str(target)).returncode, 0)
        self.assertEqual(target.read_text(), PLUGIN.read_text())
        self.assertFalse(self.plugin.exists())

    def test_plugin_is_one_dependency_free_module(self):
        imports = [line for line in PLUGIN.read_text().splitlines() if line.startswith("import ")]
        self.assertTrue(imports)
        self.assertTrue(all('from "node:' in line for line in imports), imports)


class ClaudeStaysDefault(Scratch):
    def test_no_harness_still_means_claude_settings(self):
        result = self.gf("status", "--json")
        self.assertEqual(json.loads(result.stdout)["settings"], str(self.home / ".claude/settings.json"))
        result = self.gf("status", "--harness", "claude", "--json")
        self.assertEqual(json.loads(result.stdout)["settings"], str(self.home / ".claude/settings.json"))


if __name__ == "__main__":
    unittest.main()
