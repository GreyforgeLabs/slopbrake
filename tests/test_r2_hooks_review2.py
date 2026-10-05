"""Round 2 review of the hooks (FIX-SPEC B11, B18): linked worktrees share their repository's trust, snapshots never
take the index lock, `user-hooks status` stays a health check, and the `slopbrake hook` alias never exits 2."""
import json
import os
import unittest
from unittest import mock

from test_cli_hooks import GIT_ID, sh
from test_r2_hooks import R2

from slopbrake import hooks


class WorktreeTrust(R2):
    """B11 (b): trust names a repository, so its linked worktrees (.claude/worktrees/<x>) are gated too."""

    def worktree(self, repo, name="feat"):
        tree = repo / ".claude/worktrees" / name
        result = sh(["git", "worktree", "add", "-q", str(tree), "-b", name], repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return tree

    def test_a_worktree_of_a_trusted_repo_is_gated(self):
        shop = self.repo("shop")
        self.trust(shop)
        tree = self.worktree(shop)
        (tree / "src/a.py").write_text("x = 2\n")
        self.edit(tree / "src/a.py")
        self.assertEqual(self.touched(), [str(tree)])
        result = self.hook("stop", {})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"red in {tree}", result.stderr)
        status = json.loads(self.gf("user-hooks", "status", "--settings", str(self.settings), "--json").stdout)
        self.assertEqual(status["untrusted"], [])

    def test_a_worktree_of_an_untrusted_repo_stays_untrusted(self):
        shop = self.repo("shop")
        tree = self.worktree(shop)
        self.edit(tree / "src/a.py")
        self.assertEqual(self.hook("stop", {}).returncode, 0)
        self.assertFalse(self.marker.exists())
        status = json.loads(self.gf("user-hooks", "status", "--settings", str(self.settings), "--json").stdout)
        self.assertEqual(status["untrusted"], [str(tree)])

    def test_trusting_or_untrusting_from_a_worktree_names_the_main_checkout(self):
        shop = self.repo("shop")
        tree = self.worktree(shop)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / "config")}):
            self.assertEqual(hooks.trust(tree / "src"), shop)
            self.assertTrue(hooks.is_trusted(tree))
            self.assertTrue(hooks.untrust(tree))
            self.assertFalse(hooks.is_trusted(shop))
            self.assertFalse(hooks.is_trusted(self.base / "gone"))


class NoIndexLock(R2):
    """B11 (c): the PreToolUse/PostToolUse snapshot must not lock or rewrite .git/index (a parallel `git add`
    or the operator's own git command would fail on index.lock)."""

    def test_snapshot_leaves_the_index_alone(self):
        shop = self.repo("shop")
        index = shop / ".git/index"
        old = index.stat().st_mtime - 100
        os.utime(index, (old, old))
        os.utime(shop / "src/a.py", (old + 50, old + 50))  # same content, stale stat info in the index
        before = index.read_bytes(), index.stat().st_mtime_ns
        self.assertIsNotNone(hooks.snapshot(shop))
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)
        self.assertFalse((shop / ".git/index.lock").exists())
        sh(["git", *GIT_ID, "status"], shop)  # control: a plain status does refresh this index
        self.assertNotEqual(index.stat().st_mtime_ns, before[1])


class StatusExit(R2):
    """`user-hooks status` is a health check: 0 only when the hooks are installed and can fire (B18 changed the
    wording only)."""

    def test_nothing_installed_exits_1_with_an_informational_note(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{}\n")
        result = self.gf("user-hooks", "status", "--settings", str(self.settings), "--json")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(json.loads(result.stdout)["installed"])


class HookAlias(R2):
    """B11 (a): `slopbrake hook <unknown>` answers like slopbrake-hook (exit 1), never argparse's blocking 2."""

    def test_unknown_event_through_the_alias_exits_1(self):
        result = self.gf("hook", "bogus", stdin="{}")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("usage: slopbrake-hook", result.stderr)
        self.assertEqual(self.gf("hook", stdin="{}").returncode, 1)


if __name__ == "__main__":
    unittest.main()
