"""The hooks run on every agent tool call: what they import and spawn, the one git status a snapshot costs, and
edits recorded against the repo's own paths (also through a symlinked directory)."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from test_cli_hooks import GIT_ID, HOME, sh
from test_git_guard import GUARD, HOOKS, load_guard
from test_r2_hooks import R2

from slopbrake import hooks

# Modules only git, the Stop gate or the installer need: a hook call that does none of those never loads them.
DEFERRED = ("subprocess", "tempfile", "shutil", "hashlib", "argparse", "copy", "signal")


def loaded_after(code: str, env: dict | None = None) -> list[str]:
    """The DEFERRED modules a fresh interpreter has loaded after running code (with slopbrake.hooks as h)."""
    probe = (f"import sys; sys.path.insert(0, {str(HOME)!r}); import slopbrake.hooks as h\n{code}\n"
             f"print(sorted(m for m in {DEFERRED!r} if m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False,
                            env={**os.environ, **(env or {})})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1].replace("'", '"'))


class Startup(R2):
    def test_importing_the_hooks_loads_no_deferred_module(self):
        self.assertEqual(loaded_after("pass"), [])

    def test_a_bash_call_outside_managed_repos_loads_no_deferred_module(self):
        plain = self.repo("plain", managed=False)
        payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(plain), "tool_use_id": "t1",
                              "tool_input": {"command": "git status && ls src"}})
        code = (f"assert h.run('pre-tool-use', {payload!r}) == 0\n"
                f"assert h.run('post-tool-use', {payload!r}) == 0\n"
                "assert h.run('stop', '{\"session_id\": \"s1\"}') == 0")
        self.assertEqual(loaded_after(code, {"XDG_STATE_HOME": str(self.state)}), [])

    def test_the_guard_module_needs_no_argparse_to_judge(self):
        code = (f"import importlib.machinery, types; g = types.ModuleType('g')\n"
                f"importlib.machinery.SourceFileLoader('g', {str(GUARD)!r}).exec_module(g)\n"
                "assert g.check_command('git push -f', '/', 'always')")
        self.assertEqual(loaded_after(code), [])


class RepoState(R2):
    def test_one_status_reads_head_and_every_kind_of_change(self):
        shop = self.repo("shop")
        (shop / "old name.py").write_text("a long enough body for git to see a rename\n" * 3)
        (shop / "gone.py").write_text("y = 1\n")
        (shop / "moved.py").write_text("another long enough body for a worktree rename\n" * 3)
        sh(["git", "add", "-A"], shop)
        sh(["git", *GIT_ID, "commit", "-q", "-m", "more"], shop)
        sh(["git", "mv", "old name.py", "new name.py"], shop)  # staged rename: "2 R." with the old path after it
        (shop / "moved.py").rename(shop / "to.py")
        sh(["git", "add", "-N", "to.py"], shop)  # a worktree rename: "2 .R" (porcelain v1 put the R second)
        (shop / "gone.py").unlink()
        (shop / "src/deep/er").mkdir(parents=True)
        (shop / "src/deep/er/new.py").write_text("z = 1\n")
        head, files = hooks.repo_state(shop)
        self.assertEqual(head, sh(["git", "rev-parse", "HEAD"], shop).stdout.strip())
        self.assertEqual(sorted(files), ["gone.py", "new name.py", "src/deep/er/new.py", "to.py"])
        self.assertEqual(files["gone.py"], "-")
        info = (shop / "new name.py").stat()
        self.assertEqual(files["new name.py"], f"{info.st_size}:{info.st_mtime_ns}")

    def test_before_the_first_commit_head_is_empty(self):
        fresh = self.base / "fresh"
        fresh.mkdir()
        sh(["git", "init", "-q"], fresh)
        (fresh / "a.py").write_text("x = 1\n")
        self.assertEqual(hooks.repo_state(fresh)[0], "")
        self.assertEqual(sorted(hooks.repo_state(fresh)[1]), ["a.py"])

    def test_a_failing_git_is_no_state(self):
        self.assertIsNone(hooks.repo_state(self.base))  # not a repository


class EditAttribution(R2):
    def recorded(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}):
            return hooks.session_record("s1")

    def test_an_edit_through_a_symlinked_directory_is_the_repos_own_file(self):
        shop = self.repo("shop")
        self.trust(shop)
        (self.base / "link").symlink_to(shop)
        self.edit(self.base / "link/src/a.py", cwd=self.base / "link")
        self.assertEqual(self.recorded()[0], {str(shop): ["src/a.py"]})
        result = self.hook("stop", {})  # HEAD never moved: only the attributed edit makes the gate run
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"red in {shop}", result.stderr)

    def test_head_is_read_the_first_time_a_repo_is_edited_only(self):
        shop = self.repo("shop")
        first = sh(["git", "rev-parse", "HEAD"], shop).stdout.strip()
        payload = {"session_id": "s1", "tool_name": "Edit", "cwd": str(shop),
                   "tool_input": {"file_path": str(shop / "src/a.py")}}
        calls = []
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}), \
                mock.patch.object(hooks, "head_commit", side_effect=lambda repo: calls.append(repo) or first):
            hooks.post_tool_use(payload)
            sh(["git", *GIT_ID, "commit", "-q", "-am", "next"], shop)
            hooks.post_tool_use(payload)
        self.assertEqual(calls, [shop])
        self.assertEqual(self.recorded()[1], {str(shop): first})


class GuardCommandLine(R2):
    def guard(self, *args, stdin='{"tool_input": {"command": "git push -f"}, "cwd": "/"}'):
        return sh([sys.executable, str(GUARD), *args], self.base, stdin=stdin)

    def test_scope_spellings_and_bad_usage(self):
        self.assertEqual(self.guard("--scope=always").returncode, 2)
        self.assertEqual(self.guard("--scope", "managed").returncode, 0)  # "/" is no managed repo
        bad = self.guard("--scope", "everywhere")
        self.assertEqual(bad.returncode, 2)  # bad usage blocks, like argparse did
        self.assertIn("usage: git_guard.py", bad.stderr)
        self.assertEqual(self.guard("--help").returncode, 0)

    def test_the_hand_written_option_parser_matches_its_usage(self):
        allowed = '{"tool_input": {"command": "git status"}, "cwd": "/"}'
        self.assertEqual(self.guard(stdin=allowed).returncode, 0)  # no option: scope always, an allowed command
        self.assertEqual(self.guard().returncode, 2)
        self.assertEqual(self.guard("--scope=managed").returncode, 0)  # "/" is no managed repo
        for bad in (["--scope"], ["--scope=managed", "extra"], ["--bogus", "managed"], ["--scope", "managed", "x"]):
            result = self.guard(*bad)
            self.assertEqual(result.returncode, 2, bad)
            self.assertIn("usage: git_guard.py [--scope always|managed]", result.stderr, bad)
        for flag in ("-h", "--help"):
            result = self.guard(flag)
            self.assertEqual(result.returncode, 0, flag)
            self.assertIn("usage: git_guard.py [--scope always|managed]", result.stdout)
        self.assertIn("block destructive git commands in agent sessions", self.guard("-h").stdout)
        with mock.patch("sys.stdout"):
            self.assertEqual(load_guard().main(["-h"]), 0)  # an exit code, not None

    def test_the_project_hook_caches_bytecode_outside_the_repo_and_ignores_the_cwd(self):
        hooks_dir = self.base / "repo/.claude/hooks"
        hooks_dir.mkdir(parents=True)
        for name in ("block-dangerous-git.sh", "git_guard.py"):
            (hooks_dir / name).write_bytes((HOOKS / name).read_bytes())
            (hooks_dir / name).chmod(0o755)
        cwd = self.base / "session"
        cwd.mkdir()
        (cwd / "json.py").write_text("raise SystemExit('a json.py in the cwd was imported')\n")
        cache = self.base / "cache"
        for env in ({"XDG_CACHE_HOME": str(cache)}, {"XDG_CACHE_HOME": "/dev/null/unwritable"}):
            for command, code in (("git push -f", 2), ("git status", 0)):
                result = sh([str(hooks_dir / "block-dangerous-git.sh")], cwd, env={**os.environ, **env},
                            stdin=json.dumps({"tool_input": {"command": command}}))
                self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual([p.name.split(".")[0] for p in cache.rglob("git_guard.*.pyc")], ["git_guard"])
        self.assertEqual(sorted(p.name for p in hooks_dir.iterdir()), ["block-dangerous-git.sh", "git_guard.py"])


if __name__ == "__main__":
    unittest.main()
