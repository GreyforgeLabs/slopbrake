"""User-level Claude Code hooks (`slopbrake hook`, `slopbrake user-hooks`), never touching ~/.claude/settings.json."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))

from slopbrake import hooks

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
        self.settings = self.base / "home/.claude/settings.json"
        self.marker = self.base / "stop-ran"
        # The `slopbrake` and `slopbrake-hook` Claude Code would run: this checkout, not whatever build is installed.
        self.good_bin = self.shim("good", f'PYTHONPATH={HOME} exec {sys.executable} -m slopbrake "$@"')
        self.shim("good", f'PYTHONPATH={HOME} exec {sys.executable} -m slopbrake.hooks "$@"', "slopbrake-hook")

    def shim(self, name, body, program="slopbrake"):
        bindir = self.base / "bin" / name
        bindir.mkdir(parents=True, exist_ok=True)
        (bindir / program).write_text(f"#!/bin/sh\n{body}\n")
        (bindir / program).chmod(0o755)
        return bindir

    def env(self, **extra):
        return {**os.environ, "PYTHONPATH": str(HOME), "HOME": str(self.base / "home"),
                "PATH": f"{self.good_bin}{os.pathsep}{os.environ['PATH']}",
                "XDG_STATE_HOME": str(self.state), "XDG_CONFIG_HOME": str(self.base / "config"),
                "CLAUDE_PROJECT_DIR": str(self.base), **extra}

    def gf(self, *args, stdin=None, **env):
        return sh([sys.executable, "-m", "slopbrake", *args], self.base, env=self.env(**env), stdin=stdin)

    def repo(self, name, managed=True):
        root = self.base / name
        root.mkdir()
        sh(["git", "init", "-q", "-b", "main"], root)
        (root / "src").mkdir()
        (root / "src/a.py").write_text("x = 1\n")
        if managed:
            (root / ".claude/hooks").mkdir(parents=True)
            (root / ".claude/slopbrake.json").write_text('{"stack": "python", "kit": "slopbrake"}\n')
            stub = root / ".claude/hooks/require-green.sh"
            stub.write_text(f'#!/bin/sh\necho "$CLAUDE_PROJECT_DIR" >> {self.marker}\necho "red in $CLAUDE_PROJECT_DIR" >&2\n'
                            "exit 2\n")
            stub.chmod(0o755)
        sh(["git", "add", "-A"], root)
        sh(["git", *GIT_ID, "commit", "-q", "-m", "init"], root)
        return root

    def touched(self, session="s1"):
        path = self.state / "slopbrake/sessions" / f"{session}.json"
        return json.loads(path.read_text()).get("repos", []) if path.is_file() else []

    def hook(self, event, payload, **env):
        return self.gf("hook", event, stdin=json.dumps({"session_id": "s1", **payload}), **env)


class UserHooks(Scratch):
    def test_install_merges_is_idempotent_and_uninstall_removes_only_ours(self):
        self.settings.parent.mkdir(parents=True)
        mine = {"type": "command", "command": "my-linter"}
        self.settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [mine]}]}}))
        for _ in range(2):
            self.assertEqual(self.gf("user-hooks", "install", "--settings", str(self.settings)).returncode, 0)
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["model"], "x")
        commands = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
        self.assertEqual(commands, ["my-linter", "slopbrake-hook stop"])  # B11 (a): its own console script
        for event in ("PreToolUse", "PostToolUse"):
            self.assertEqual(sum(h["command"].startswith("slopbrake-hook") for e in data["hooks"][event]
                                 for h in e["hooks"]), 1)
        status = json.loads(self.gf("user-hooks", "status", "--settings", str(self.settings), "--json").stdout)
        self.assertTrue(status["installed"])
        self.assertEqual(self.gf("user-hooks", "uninstall", "--settings", str(self.settings)).returncode, 0)
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "x", "hooks": {"Stop": [{"hooks": [mine]}]}})
        # B18: nothing installed is informational, not a failure.
        result = self.gf("user-hooks", "status", "--settings", str(self.settings), "--json")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["installed"])

    def test_unparsable_settings_are_left_alone(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{ // nope\n")
        result = self.gf("user-hooks", "install", "--settings", str(self.settings))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.settings.read_text(), "{ // nope\n")

    def test_install_refuses_a_slopbrake_on_path_that_cannot_run_hook(self):
        # A broken build that answers with exit 2, which Claude Code reads as "block": every
        # Bash call and every Stop would be refused.
        old = self.shim("old", "echo \"slopbrake-hook: error\" >&2; exit 2", "slopbrake-hook")
        path = f"{old}{os.pathsep}{os.environ['PATH']}"
        result = self.gf("user-hooks", "install", "--settings", str(self.settings), PATH=path)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("slopbrake-hook", result.stderr)
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.gf("user-hooks", "install", "--settings", str(self.settings)).returncode, 0)
        result = self.gf("user-hooks", "status", "--settings", str(self.settings), "--json", PATH=path)
        self.assertEqual(result.returncode, 1)
        status = json.loads(result.stdout)
        self.assertTrue(status["installed"])
        self.assertFalse(status["runnable"])
        self.assertTrue(json.loads(self.gf("user-hooks", "status", "--settings", str(self.settings), "--json")
                                   .stdout)["runnable"])

    def test_runnable_probe_ignores_the_installers_pythonpath(self):
        # Claude Code runs the hook without the installer's PYTHONPATH: an old build that only
        # works with a dev checkout on PYTHONPATH must not pass the probe.
        dev_only = self.shim("dev-only", f'[ -n "$PYTHONPATH" ] && exec {sys.executable} -m slopbrake.hooks "$@"; exit 2',
                             "slopbrake-hook")
        path = f"{dev_only}{os.pathsep}{os.environ['PATH']}"
        result = self.gf("user-hooks", "install", "--settings", str(self.settings), PATH=path)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(self.settings.exists())

    def test_install_creates_a_missing_settings_file(self):
        self.assertEqual(self.gf("user-hooks", "install", "--settings", str(self.settings)).returncode, 0)
        self.assertIn("PreToolUse", json.loads(self.settings.read_text())["hooks"])


class PostToolUse(Scratch):
    def test_edits_and_bash_targets_in_managed_repos_are_recorded(self):
        shop, web, other = self.repo("shop"), self.repo("web"), self.repo("other", managed=False)
        self.assertEqual(self.hook("post-tool-use", {"tool_name": "Edit", "cwd": str(self.base),
                                                     "tool_input": {"file_path": str(shop / "src/a.py")}}).returncode, 0)
        self.hook("post-tool-use", {"tool_name": "Write", "cwd": str(self.base),
                                    "tool_input": {"file_path": str(other / "src/new.py")}})
        # B11 (c): PreToolUse snapshots the Bash targets; PostToolUse records the ones that changed.
        bash = {"tool_name": "Bash", "cwd": str(other), "tool_use_id": "b1",
                "tool_input": {"command": f"cd {web}/src && touch new.py; git -C {other} status"}}
        self.assertEqual(self.hook("pre-tool-use", bash).returncode, 0)
        (web / "src/new.py").touch()
        self.hook("post-tool-use", bash)
        self.assertEqual(self.touched(), sorted([str(shop), str(web)]))

    def test_bash_targets_without_a_snapshot_are_not_recorded(self):
        # B11 (c), round 2 review: with no PreToolUse snapshot there is no evidence of a change, and the
        # payload's cwd may already be a repo a `cd` moved into; recording it would gate the operator's WIP.
        shop, web = self.repo("shop"), self.repo("web")
        self.hook("post-tool-use", {"tool_name": "Bash", "cwd": str(shop / "src"), "tool_input": {"command": "true"}})
        self.hook("post-tool-use", {"tool_name": "Bash", "cwd": str(self.base),
                                    "tool_input": {"command": f'git -C "{web}" commit -m x'}})
        self.assertEqual(self.touched(), [])


class Stop(Scratch):
    def test_runs_require_green_in_each_touched_repo(self):
        shop = self.repo("shop")
        self.gf("user-hooks", "trust", str(shop))  # B11 (b): only trusted repos' scripts run
        self.hook("post-tool-use", {"tool_name": "Edit", "tool_input": {"file_path": str(shop / "src/a.py")},
                                    "cwd": str(self.base)})
        result = self.hook("stop", {"stop_hook_active": False})
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"red in {shop}", result.stderr)
        self.assertEqual(self.marker.read_text().split(), [str(shop)])

    def test_skips_the_project_repo_when_its_own_hooks_are_wired(self):
        shop = self.repo("shop")
        self.gf("user-hooks", "trust", str(shop))  # B11 (b): only trusted repos' scripts run
        self.hook("post-tool-use", {"tool_name": "Edit", "tool_input": {"file_path": str(shop / "src/a.py")},
                                    "cwd": str(shop)})
        (shop / ".claude/settings.json").write_text(KIT_SETTINGS)
        (shop / ".claude/hooks/block-dangerous-git.sh").write_text("#!/bin/sh\n")
        (shop / ".claude/hooks/block-dangerous-git.sh").chmod(0o755)
        self.assertEqual(self.hook("stop", {}, CLAUDE_PROJECT_DIR=str(shop)).returncode, 0)
        self.assertFalse(self.marker.exists())
        (shop / ".claude/settings.json").write_text("{}")  # project hooks not wired: the user hook must gate it
        self.assertEqual(self.hook("stop", {}, CLAUDE_PROJECT_DIR=str(shop)).returncode, 2)

    def test_nothing_touched_is_a_no_op(self):
        self.assertEqual(self.hook("stop", {}).returncode, 0)


class PreToolUse(Scratch):
    def test_outside_managed_repos_nothing_is_blocked_even_on_bad_input(self):
        other = self.repo("other", managed=False)
        self.assertEqual(self.hook("pre-tool-use", {"tool_name": "Bash", "cwd": str(other),
                                                    "tool_input": {"command": "git push --force"}}).returncode, 0)
        self.assertEqual(self.gf("hook", "pre-tool-use", stdin="not json").returncode, 0)
        self.assertEqual(self.gf("hook", "stop", stdin="not json").returncode, 0)

    def test_force_push_in_a_managed_repo_is_blocked(self):
        shop = self.repo("shop")
        result = self.hook("pre-tool-use", {"tool_name": "Bash", "cwd": str(shop),
                                            "tool_input": {"command": "git push --force origin main"}})
        self.assertEqual(result.returncode, 2)

    def test_guard_is_applied_in_managed_scope(self):
        guard = self.base / "git_guard.py"
        guard.write_text("def check_command(command, cwd, scope):\n"
                         "    return f'blocked ({scope}) in {cwd}' if 'reset --hard' in command else None\n")
        payload = {"tool_name": "Bash", "cwd": "/somewhere", "tool_input": {"command": "git reset --hard"}}
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(hooks.pre_tool_use(payload, guard=guard), 2)
        self.assertIn("blocked (managed) in /somewhere", err.getvalue())
        payload["tool_input"]["command"] = "git status"
        self.assertEqual(hooks.pre_tool_use(payload, guard=guard), 0)


if __name__ == "__main__":
    unittest.main()
