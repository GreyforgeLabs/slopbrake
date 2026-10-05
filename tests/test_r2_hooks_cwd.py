"""Round 2 review follow-up (FIX-SPEC B11 c, f): PostToolUse judges a Bash call from the cwd PreToolUse saw,
so a `cd` Claude Code persisted into a repo never makes a read-only command look like a change; the managed
scope of an unparseable nested command follows its known directory."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_cli_hooks import sh
from test_git_guard import load_guard
from test_r2_hooks import R2


class PreToolUseCwd(R2):
    """B11 (c): a repo is recorded only when the agent changed it, wherever the shell's cwd ends up."""

    def setUp(self):
        super().setUp()
        self.shop = self.repo("shop")
        self.trust(self.shop)
        (self.shop / "src/a.py").write_text("x = 2  # operator WIP\n")  # red WIP the agent must not inherit

    def call(self, command, cwd, post_cwd, pre=True, run=True, tool_use_id="t1", env=None):
        """A Bash call whose PostToolUse reports post_cwd: Claude Code persists the shell's cwd after a `cd`."""
        payload = {"session_id": "s1", "tool_name": "Bash", "cwd": str(cwd), "tool_use_id": tool_use_id,
                   "tool_input": {"command": command}}
        if pre:
            self.assertEqual(self.gf("hook", "pre-tool-use", stdin=json.dumps(payload), **(env or {})).returncode, 0)
        if run:
            sh(["bash", "-c", command], cwd, env={**os.environ, **(env or {})})
        post = dict(payload, cwd=str(post_cwd))
        self.assertEqual(self.gf("hook", "post-tool-use", stdin=json.dumps(post), **(env or {})).returncode, 0)

    def test_a_dynamic_cd_into_a_repo_that_only_reads_does_not_record(self):
        home = self.base / "home"
        home.mkdir(parents=True, exist_ok=True)
        for i, command in enumerate([f'cd "$(echo {self.shop})" && git status', f'D={self.shop}; cd "$D" && ls']):
            self.call(command, home, self.shop, tool_use_id=f"t{i}")
        self.assertEqual(self.touched(), [])
        self.assertEqual(self.hook("stop", {}).returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_a_post_tool_use_without_a_snapshot_does_not_record(self):
        self.call("ls", self.shop, self.shop, pre=False, run=False)
        self.assertEqual(self.touched(), [])

    def test_a_fresh_clone_that_is_changed_still_records(self):
        upstream = self.repo("upstream")
        work = self.base / "work"
        work.mkdir()
        self.call(f"git clone -q {upstream} d && cd d && echo y >> src/a.py", work, work / "d")
        self.assertEqual(self.touched(), [str((work / "d").resolve())])

    def test_relative_and_variable_paths_outside_the_repo_record(self):
        elsewhere = self.base / "norepo"
        elsewhere.mkdir()
        self.call("echo q >> ../shop/src/a.py", elsewhere, elsewhere, tool_use_id="t1")
        self.assertEqual(self.touched(), [str(self.shop)])
        other = self.repo("other")
        env = {"SLOPBRAKE_TEST_ROOT": str(self.base)}
        self.call("echo q >> $SLOPBRAKE_TEST_ROOT/other/src/a.py", elsewhere, elsewhere, tool_use_id="t2", env=env)
        self.assertEqual(self.touched(), sorted([str(self.shop), str(other)]))


class NestedManagedScope(unittest.TestCase):
    """B11 (f): an unparseable nested command whose directory is a known managed repo stays blocked."""

    def test_unparseable_alias_body_under_git_c_managed_is_blocked_from_a_plain_session(self):
        guard = load_guard()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        managed, plain = root / "managed", root / "plain"
        for repo in (managed, plain):
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (managed / ".claude").mkdir()
        (managed / ".claude/slopbrake.json").write_text('{"stack": "python"}\n')
        body = """!echo "${x:-"}"}"; git reset --hard"""  # does not parse: only the fallback pattern sees it
        command = f"git -C {managed} -c alias.x='{body}' x"
        self.assertIsNotNone(guard.check_command(command, str(plain), "managed"))
        unknown = f"git -C \"$REPO\" -c alias.x='{body}' x"
        self.assertIsNone(guard.check_command(unknown, str(plain), "managed"))  # unknown directory: B11 (f)
        self.assertIsNotNone(guard.check_command(unknown, str(managed), "managed"))


if __name__ == "__main__":
    unittest.main()
