"""Behaviour that spans kit areas: the installed kit is never treated as the repo's own code."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
GUARDS = HOME / "slopbrake/kit/common/scripts/slopbrake"
GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]


def sh(cmd, cwd, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def commit(self, message):
        sh(["git", "add", "-A"], self.root)
        self.assertEqual(sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", message], self.root).returncode, 0)

    def slopbrake(self, *args):
        return sh([sys.executable, "-m", "slopbrake", *args], self.root, dict(os.environ, PYTHONPATH=str(HOME)))


class KitIsNotRepoCode(Scratch):
    def test_mutation_never_mutates_the_installed_kit(self):
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("tests/test_ok.py", "def test_ok():\n    assert 1 + 1 == 2\n")
        self.commit("init")
        # A kit upgrade changes the hook and ESLint-rule code the repo carries; that is not code under test.
        self.write(".claude/hooks/git_guard.py", "def judge(x):\n    return x > 1\n")
        self.write("eslint-rules/helper.py", "def judge(x):\n    return x > 1\n")
        result = sh([sys.executable, str(GUARDS / "mutation_py.py"), "--base", "HEAD",
                     "--test-cmd", "true"], self.root)
        self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
        self.assertIn("no changed Python source lines", result.stdout)

    def test_typescript_init_ignores_the_kits_bytecode(self):
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("package.json", '{"name": "t"}\n')
        self.write("tsconfig.json", "{}\n")
        self.commit("init")
        result = self.slopbrake("init", ".", "--stack", "typescript")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("__pycache__/", (self.root / ".gitignore").read_text().splitlines())
        # Both ESLint rules the kit ships are in the wiring instructions.
        self.assertIn("slopbrake/expect-in-test", result.stdout)


HOOK = HOME / "slopbrake/kit/common/.claude/hooks/require-green.sh"
FULL_IS_RED = '#!/bin/sh\n[ "$1" = --fast ] && exit 0\necho "full gate ran"\nexit 1\n'


class StopHookOnNewBranches(Scratch):
    def stop(self):
        return subprocess.run([str(HOOK)], cwd=self.root, input='{"session_id": "s1"}', capture_output=True, text=True,
                              env=dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root)), check=False)

    def setUp(self):
        super().setUp()
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("scripts/check", FULL_IS_RED).chmod(0o755)
        self.commit("init")

    def test_commits_on_a_branch_with_no_green_run_get_the_full_gate(self):
        sh(["git", "switch", "-q", "-c", "feat"], self.root)
        self.write("shop.py", "def one():\n    return 1\n")
        self.commit("feat")
        result = self.stop()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("full gate ran", result.stderr)

    def test_the_default_branch_with_nothing_new_lets_the_agent_stop(self):
        self.assertEqual(self.stop().returncode, 0)


if __name__ == "__main__":
    unittest.main()
