"""The user-level Stop gate judges this session's own work, never another session's uncommitted files."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
KIT = HOME / "slopbrake/kit/common"
GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]
# A gate that is red while a file named RED exists, or while any .py file says "bad".
GATE = """#!/usr/bin/env bash
cd "$(dirname "$0")/.."
if [ -e RED ] || grep -rqs --include='*.py' bad .; then echo "FAIL  tests"; exit 1; fi
echo "pass  tests"
"""


def sh(cmd, cwd, env=None, stdin=None):
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False)


class StopScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / ".claude/hooks").mkdir(parents=True)
        (self.repo / ".claude/slopbrake.json").write_text('{"stack": "python"}\n')
        hook = self.repo / ".claude/hooks/require-green.sh"
        hook.write_text((KIT / ".claude/hooks/require-green.sh").read_text())
        hook.chmod(0o755)
        gate = self.repo / "scripts/check"
        gate.write_text(GATE)
        gate.chmod(0o755)
        (self.repo / "app.py").write_text("x = 1\n")
        sh(["git", "init", "-q", "-b", "main"], self.repo)
        sh(["git", "add", "-A"], self.repo)
        sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", "init"], self.repo)
        sh(["git", "update-ref", "refs/slopbrake/last-green/main", "HEAD"], self.repo)
        self.env = dict(os.environ, PYTHONPATH=str(HOME), XDG_STATE_HOME=str(base / "state"),
                        XDG_CONFIG_HOME=str(base / "config"))
        self.env.pop("CLAUDE_PROJECT_DIR", None)
        self.hook("pre-tool-use", {})  # warm import; nothing recorded
        self.assertEqual(sh([sys.executable, "-m", "slopbrake", "user-hooks", "trust", str(self.repo)], base, self.env)
                         .returncode, 0)

    def hook(self, event, payload):
        payload = {"session_id": "s1", "cwd": str(self.repo), **payload}
        return sh([sys.executable, "-m", "slopbrake.hooks", event], self.repo, self.env, json.dumps(payload))

    def edit(self, rel, text):
        (self.repo / rel).write_text(text)
        self.hook("post-tool-use", {"tool_name": "Write", "tool_input": {"file_path": str(self.repo / rel)}})

    def bash(self, command, action):
        payload = {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": command}}
        self.hook("pre-tool-use", payload)
        action()
        self.hook("post-tool-use", payload)

    def test_another_sessions_red_files_do_not_hold_this_session(self):
        (self.repo / "RED").write_text("someone else's work in progress\n")
        self.edit("notes.txt", "my own docs-only edit\n")
        result = self.hook("stop", {})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_this_sessions_green_edit_passes_beside_foreign_red_files(self):
        (self.repo / "RED").write_text("someone else's work in progress\n")
        self.edit("app.py", "x = 2\n")
        result = self.hook("stop", {})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_this_sessions_red_edit_still_blocks_beside_foreign_files(self):
        (self.repo / "other.py").write_text("y = 1\n")  # foreign, untracked
        self.edit("app.py", "x = 'bad'\n")
        result = self.hook("stop", {})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("FAIL  tests", result.stderr)

    def test_a_bash_change_is_attributed_to_the_session(self):
        (self.repo / "RED").write_text("foreign\n")
        self.bash("sed -i s/1/bad/ app.py", lambda: (self.repo / "app.py").write_text("x = 'bad'\n"))
        result = self.hook("stop", {})
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_the_real_tree_is_left_exactly_as_it_was(self):
        (self.repo / "RED").write_text("foreign\n")
        self.edit("app.py", "x = 2\n")
        before = sh(["git", "status", "--porcelain"], self.repo).stdout
        self.hook("stop", {})
        self.assertEqual(sh(["git", "status", "--porcelain"], self.repo).stdout, before)
        self.assertEqual(sh(["git", "worktree", "list"], self.repo).stdout.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
