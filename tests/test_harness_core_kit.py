"""The kit's project hooks under grok (camelCase hook JSON) and init/status with git hooks disabled."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
HOOKS = HOME / "slopbrake/kit/common/.claude/hooks"
GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]
DISABLED = "git hooks are disabled (core.hooksPath=/dev/null)"


def sh(cmd, cwd, env=None, stdin=None):
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.root.mkdir()
        (self.root.parent / "home").mkdir()
        sh(["git", "init", "-q", "-b", "main"], self.root)

    def write(self, rel, text, mode=None):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        if mode:
            path.chmod(mode)

    def commit(self, message):
        sh(["git", "add", "-A"], self.root)
        self.assertEqual(sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", message], self.root).returncode, 0)

    def git(self, *args):
        return sh(["git", *args], self.root).stdout.strip()


class GrokRunsTheProjectHooks(Scratch):
    """grok runs a trusted project's .claude/settings.json hooks with camelCase JSON."""

    def guard(self, command):
        payload = {"hookEventName": "pre_tool_use", "hook_event_name": "PreToolUse", "sessionId": "g1",
                   "cwd": str(self.root), "toolName": "run_terminal_command", "toolInput": {"command": command}}
        return sh([str(HOOKS / "block-dangerous-git.sh")], self.root, stdin=json.dumps(payload))

    def test_force_push_in_a_grok_payload_is_blocked(self):
        result = self.guard("git push --force origin main")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("destructive-git guard (H5)", result.stderr)
        self.assertEqual(self.guard("git status").returncode, 0)

    def test_require_green_counts_red_stops_per_grok_session(self):
        self.write("scripts/check", "#!/bin/sh\necho red\nexit 1\n", mode=0o755)
        self.commit("red gate")
        self.write("app.py", "x = 1\n")
        payload = {"hookEventName": "stop", "hook_event_name": "Stop", "sessionId": "grok-7", "cwd": str(self.root),
                   "stopHookActive": False}
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root), SLOPBRAKE_STOP_TIMEOUT="60")
        result = sh([str(HOOKS / "require-green.sh")], self.root, env=env, stdin=json.dumps(payload))
        self.assertEqual(result.returncode, 2, result.stderr)
        state = Path(self.git("rev-parse", "--absolute-git-dir")) / "slopbrake"
        self.assertEqual(sorted(p.name for p in state.glob("stop-red-*")), ["stop-red-grok-7"])


class HooksDisabled(Scratch):
    def gf(self, *args):
        base = self.root.parent
        env = dict(os.environ, PYTHONPATH=str(HOME), XDG_CONFIG_HOME=str(base / "config"), HOME=str(base / "home"),
                   UV_CACHE_DIR=os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv")))
        return sh([sys.executable, "-m", "slopbrake", *args], self.root, env=env)

    def python_repo(self):
        self.write("pyproject.toml", "[project]\nname = 'shop'\n")
        self.write("shop.py", "def one():\n    return 1\n")
        self.write("tests/test_shop.py", "import unittest\nfrom shop import one\n\n\nclass T(unittest.TestCase):\n"
                                         "    def test_one(self):\n        self.assertEqual(one(), 1)\n")
        self.commit("app")
        sh(["git", "config", "core.hooksPath", "/dev/null"], self.root)

    def test_init_leaves_disabled_hooks_alone_and_says_how_to_enable_them(self):
        self.python_repo()
        result = self.gf("init", str(self.root), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(self.git("config", "--get", "core.hooksPath"), "/dev/null")
        self.assertNotIn("existing hooks", report["hooks"])
        self.assertIn("disabled", report["hooks"])
        self.assertIn("hooks are disabled in this repo (core.hooksPath=/dev/null): enable with git config "
                      "core.hooksPath .githooks", report["next_steps"])

    def test_status_reports_disabled_hooks_as_a_gap(self):
        self.python_repo()
        self.assertEqual(self.gf("init", str(self.root)).returncode, 0)
        self.commit("install slopbrake")
        gaps = json.loads(self.gf("status", str(self.root), "--json").stdout)[0]["gaps"]
        self.assertIn(DISABLED, gaps)
        self.assertFalse([g for g in gaps if "does not exist" in g], gaps)
        sh(["git", "config", "core.hooksPath", ".githooks"], self.root)
        gaps = json.loads(self.gf("status", str(self.root), "--json").stdout)[0]["gaps"]
        self.assertNotIn(DISABLED, gaps)


if __name__ == "__main__":
    unittest.main()
