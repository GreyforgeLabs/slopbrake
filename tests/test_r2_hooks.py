"""Round 2 (FIX-SPEC B11, B18): the user-level hooks' entry point, trust store, change-based recording,
project skip, Stop budget, managed-scope guard, stdin decoding and settings file handling."""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from test_cli_hooks import GIT_ID, HOME, KIT_SETTINGS, Scratch, sh
from test_git_guard import load_guard

from slopbrake import hooks


class R2(Scratch):
    def trust(self, repo):
        self.assertEqual(self.gf("user-hooks", "trust", str(repo)).returncode, 0)

    def bash(self, command, cwd, session="s1", tool_use_id="t1"):
        """A Bash call the way Claude Code reports it: PreToolUse, the command itself, PostToolUse."""
        payload = {"session_id": session, "tool_name": "Bash", "cwd": str(cwd), "tool_use_id": tool_use_id,
                   "tool_input": {"command": command}}
        self.assertEqual(self.gf("hook", "pre-tool-use", stdin=json.dumps(payload)).returncode, 0)
        sh(["bash", "-c", command], cwd)
        self.assertEqual(self.gf("hook", "post-tool-use", stdin=json.dumps(payload)).returncode, 0)

    def edit(self, path, cwd=None):
        return self.hook("post-tool-use", {"tool_name": "Edit", "cwd": str(cwd or self.base),
                                           "tool_input": {"file_path": str(path)}})

    def wire(self, repo):
        (repo / ".claude/settings.json").write_text(KIT_SETTINGS)
        (repo / ".claude/hooks/block-dangerous-git.sh").write_text("#!/bin/sh\n")
        (repo / ".claude/hooks/block-dangerous-git.sh").chmod(0o755)


class EntryPoint(R2):
    """B11 (a): settings run `slopbrake-hook <event>`; an install without it exits 127, never 2."""

    def test_the_console_script_is_declared(self):
        scripts = tomllib.loads((HOME / "pyproject.toml").read_text())["project"]["scripts"]
        self.assertEqual(scripts["slopbrake-hook"], "slopbrake.hooks:main")
        self.assertEqual(scripts["slopbrake"], "slopbrake.cli:main")

    def test_install_wires_slopbrake_hook_and_replaces_the_legacy_entries(self):
        self.settings.parent.mkdir(parents=True)
        legacy = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "slopbrake hook pre-tool-use"}]}],
                  "Stop": [{"hooks": [{"type": "command", "command": "my-linter"},
                                      {"type": "command", "command": "slopbrake hook stop", "timeout": 600}]}]}
        self.settings.write_text(json.dumps({"hooks": legacy}))
        self.assertEqual(self.gf("user-hooks", "install", "--settings", str(self.settings)).returncode, 0)
        data = json.loads(self.settings.read_text())
        found = {event: [h["command"] for e in entries for h in e["hooks"]] for event, entries in data["hooks"].items()}
        self.assertEqual(found["PreToolUse"], ["slopbrake-hook pre-tool-use"])
        self.assertEqual(found["PostToolUse"], ["slopbrake-hook post-tool-use"])
        self.assertEqual(found["PostToolUseFailure"], ["slopbrake-hook post-tool-use"])
        self.assertEqual(found["Stop"], ["my-linter", "slopbrake-hook stop"])
        self.assertEqual(self.gf("user-hooks", "uninstall", "--settings", str(self.settings)).returncode, 0)
        self.assertEqual(json.loads(self.settings.read_text()),
                         {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-linter"}]}]}})

    def test_install_refuses_without_slopbrake_hook_on_path(self):
        only_cli = self.shim("only-cli", f'PYTHONPATH={HOME} exec {sys.executable} -m slopbrake "$@"')
        path = f"{only_cli}{os.pathsep}/usr/bin{os.pathsep}/bin"
        result = self.gf("user-hooks", "install", "--settings", str(self.settings), PATH=path)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("slopbrake-hook", result.stderr)
        self.assertFalse(self.settings.exists())

    def test_unknown_event_never_blocks_and_the_old_alias_still_works(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(hooks.main(["bogus"]), 1)
        self.assertEqual(self.gf("hook", "stop", stdin="{}").returncode, 0)
        result = sh([str(self.good_bin / "slopbrake-hook"), "stop"], self.base, env=self.env(), stdin="{}")
        self.assertEqual(result.returncode, 0, result.stderr)


class Trust(R2):
    """B11 (b): the Stop dispatcher runs repo scripts only for repos in trusted.json."""

    def test_trust_store_round_trip(self):
        shop = self.repo("shop")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / "config")}):
            self.assertFalse(hooks.is_trusted(shop))
            hooks.trust(shop / "src")  # any path in the repo trusts its toplevel
            self.assertTrue(hooks.is_trusted(shop))
            self.assertFalse(hooks.is_trusted(shop / "src"))
            store = json.loads((self.base / "config/slopbrake/trusted.json").read_text())
            self.assertEqual(store["repos"], [str(shop)])
            hooks.trust(shop)
            self.assertEqual(json.loads((self.base / "config/slopbrake/trusted.json").read_text())["repos"], [str(shop)])
            self.assertTrue(hooks.untrust(shop))
            self.assertFalse(hooks.is_trusted(shop))
            self.assertFalse(hooks.untrust(shop))

    def test_a_cloned_repo_the_agent_only_looked_at_never_runs_its_scripts(self):
        upstream = self.repo("upstream")
        sh(["git", "clone", "-q", str(upstream), "cloned"], self.base)
        cloned = self.base / "cloned"
        self.bash(f"cd {cloned} && ls && git log --oneline -1", self.base)
        self.edit(cloned / "src/a.py")  # even an edit does not make a stranger's repo trusted
        self.assertEqual(self.hook("stop", {}).returncode, 0)
        self.assertFalse(self.marker.exists())
        status = json.loads(self.gf("user-hooks", "status", "--settings", str(self.settings), "--json").stdout)
        self.assertEqual(status["untrusted"], [str(cloned)])
        self.trust(cloned)
        result = self.hook("stop", {})
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"red in {cloned}", result.stderr)
        self.assertEqual(self.gf("user-hooks", "untrust", str(cloned)).returncode, 0)
        self.assertEqual(self.hook("stop", {}).returncode, 0)
        self.assertEqual(self.marker.read_text().split(), [str(cloned)])

    def test_trust_needs_a_repo(self):
        result = self.gf("user-hooks", "trust")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        result = self.gf("user-hooks", "trust", str(self.base / "nowhere"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("not inside a git repository", result.stderr)


class RecordOnChange(R2):
    """B11 (c): a repo counts as touched only when the agent changed it."""

    def setUp(self):
        super().setUp()
        self.shop = self.repo("shop")
        self.trust(self.shop)

    def test_read_only_commands_do_not_record(self):
        (self.shop / "src/a.py").write_text("x = 2  # operator WIP\n")
        for i, command in enumerate([f"git -C {self.shop} log --oneline -3", f"cd {self.shop} && git status",
                                     f"cat {self.shop}/src/a.py", "ls"]):
            self.bash(command, self.shop if command == "ls" else self.base, tool_use_id=f"t{i}")
        self.assertEqual(self.touched(), [])
        self.assertEqual(self.hook("stop", {}).returncode, 0)

    def test_changes_through_paths_chained_cds_pushd_and_git_dir_record(self):
        other = self.repo("other")
        cases = {
            self.shop: f"sed -i s/1/2/ {self.shop}/src/a.py",
            other: f"cd {self.base} && cd other && touch src/new.py",
        }
        for i, (repo, command) in enumerate(cases.items()):
            self.bash(command, self.base, session=f"c{i}", tool_use_id=f"t{i}")
            self.assertEqual(self.touched(f"c{i}"), [str(repo)], command)
        self.bash(f"pushd {other} && echo y > src/p.py", self.base, session="c2")
        self.assertEqual(self.touched("c2"), [str(other)])
        sh(["git", "add", "-A"], other)
        self.bash(f"git --git-dir={other}/.git --work-tree={other} {' '.join(GIT_ID)} commit -qm x", self.base,
                  session="c3")
        self.assertEqual(self.touched("c3"), [str(other)])

    def test_changing_an_already_dirty_file_records(self):
        (self.shop / "src/a.py").write_text("x = 2  # operator WIP\n")
        time.sleep(0.01)
        self.bash(f"echo 'y = 3' >> {self.shop}/src/a.py", self.base)
        self.assertEqual(self.touched(), [str(self.shop)])

    def test_edits_record_directly(self):
        self.edit(self.shop / "src/a.py")
        self.assertEqual(self.touched(), [str(self.shop)])


class ProjectSkip(R2):
    """B11 (d): skip the project's repo only when CLAUDE_PROJECT_DIR is exactly its root."""

    def test_a_session_started_in_a_subdirectory_is_still_gated(self):
        shop = self.repo("shop")
        self.trust(shop)
        self.wire(shop)
        self.edit(shop / "src/a.py", cwd=shop / "src")
        result = self.hook("stop", {"cwd": str(shop / "src")}, CLAUDE_PROJECT_DIR=str(shop / "src"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.hook("stop", {"cwd": str(shop)}, CLAUDE_PROJECT_DIR=str(shop)).returncode, 0)


class StopBudget(R2):
    """B11 (e): one repo's failure or slowness never hides another's gate."""

    def gate(self, repo, body):
        (repo / ".claude/hooks/require-green.sh").write_text(f"#!/bin/sh\n{body}\n")

    def test_a_slow_gate_is_killed_at_the_budget_and_reported(self):
        slow = self.repo("slow")
        self.gate(slow, f"cat >/dev/null; sleep 30; echo late >> {self.marker}; exit 0")
        err = io.StringIO()
        env = {"XDG_CONFIG_HOME": str(self.base / "config"), "XDG_STATE_HOME": str(self.state),
               "CLAUDE_PROJECT_DIR": str(self.base)}
        with mock.patch.dict(os.environ, env), mock.patch.object(hooks, "STOP_BUDGET", 1), \
                mock.patch.object(hooks, "STOP_GRACE", 0), redirect_stderr(err):
            hooks.trust(slow)
            hooks.record("b1", {str(slow)})
            started = time.monotonic()
            code = hooks.stop({"session_id": "b1"}, "{}")
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10)
        self.assertEqual(code, 1)  # reported, but never an endless block
        self.assertIn(f"{slow}: the gate timed out", err.getvalue())
        self.assertFalse(self.marker.exists())

    def test_an_unrunnable_gate_does_not_skip_the_other_repos(self):
        broken, red = self.repo("a-broken"), self.repo("b-red")
        (broken / ".claude/hooks/require-green.sh").chmod(0o644)
        for repo in (broken, red):
            self.trust(repo)
            self.edit(repo / "src/a.py")
        result = self.hook("stop", {})
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"red in {red}", result.stderr)
        self.assertIn(f"{broken}: the gate could not run", result.stderr)

    def test_budget_exhaustion_is_reported(self):
        first, second = self.repo("a1"), self.repo("a2")
        self.gate(first, "cat >/dev/null; sleep 2; exit 0")
        err = io.StringIO()
        env = {"XDG_CONFIG_HOME": str(self.base / "config"), "XDG_STATE_HOME": str(self.state),
               "CLAUDE_PROJECT_DIR": str(self.base)}
        with mock.patch.dict(os.environ, env), mock.patch.object(hooks, "STOP_BUDGET", 1), \
                mock.patch.object(hooks, "STOP_GRACE", 5), redirect_stderr(err):
            hooks.trust(first)
            hooks.trust(second)
            hooks.record("b3", {str(first), str(second)})
            code = hooks.stop({"session_id": "b3"}, "{}")
        self.assertEqual(code, 1)
        self.assertIn(f"{second}:\nrequire-green: the 1s Stop budget was used up", err.getvalue())


class ManagedScopeGuard(unittest.TestCase):
    """B11 (f): an unknown directory or an unparseable command blocks only from a managed session cwd."""

    def setUp(self):
        self.guard = load_guard()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.managed, self.plain = root / "managed", root / "plain"
        for repo in (self.managed, self.plain):
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (self.managed / ".claude").mkdir()
        (self.managed / ".claude/slopbrake.json").write_text('{"stack": "python"}\n')

    def check(self, command, cwd):
        return self.guard.check_command(command, str(cwd), "managed")

    def test_dynamic_directories_block_only_from_a_managed_session(self):
        for command in ['cd "$(git rev-parse --show-toplevel)" && git reset --hard', "cd $WORKDIR && git clean -fdx",
                        "cd - && git checkout .", 'git -C "$REPO" push --force',
                        'for d in */; do (cd "$d" && git checkout -- .); done']:
            self.assertIsNone(self.check(command, self.plain), command)
            self.assertIsNotNone(self.check(command, self.managed), command)
        self.assertIsNotNone(self.check(f"cd {self.managed} && git reset --hard", self.plain))

    def test_unparseable_and_deep_commands_block_only_from_a_managed_session(self):
        unparseable = r'echo "${x:-"}"}"; git checkout main'
        deep = "echo " + "$(echo " * 14 + "x" + ")" * 14
        for command in (unparseable, deep):
            self.assertIsNone(self.check(command, self.plain), command)
            self.assertIsNotNone(self.check(command, self.managed), command)
        self.assertIsNotNone(self.guard.check_command(unparseable, str(self.plain), "always"))


class Stdin(R2):
    """B11 (g): bytes that are not UTF-8 never crash a hook."""

    def test_invalid_utf8_is_replaced(self):
        shop = self.repo("shop")
        payload = json.dumps({"session_id": "u", "tool_name": "Bash", "cwd": str(shop),
                              "tool_input": {"command": "git push --force origin XX"}}).encode()
        payload = payload.replace(b"XX", b"\xff\xfe")
        for argv in ([str(self.good_bin / "slopbrake-hook")], [sys.executable, "-m", "slopbrake", "hook"]):
            for event, code in (("pre-tool-use", 2), ("post-tool-use", 0), ("stop", 0)):
                result = subprocess.run([*argv, event], input=payload, cwd=self.base, env=self.env(),
                                        capture_output=True, check=False)
                self.assertEqual(result.returncode, code, (argv, event, result.stderr))
                self.assertNotIn(b"Traceback", result.stderr)
                if code:
                    self.assertIn(b"BLOCKED by the destructive-git guard", result.stderr)


class SettingsFile(R2):
    """B11 (h) and B18: file mode, disableAllHooks, and honest status when nothing is installed."""

    def test_install_and_uninstall_keep_the_file_mode(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text('{"model": "x"}\n')
        self.settings.chmod(0o600)
        for action in ("install", "uninstall"):
            self.assertEqual(self.gf("user-hooks", action, "--settings", str(self.settings)).returncode, 0)
            self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600, action)
        self.assertEqual([p.name for p in self.settings.parent.iterdir()], ["settings.json"])

    def test_status_reports_disable_all_hooks(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text('{"disableAllHooks": true}\n')
        self.assertEqual(self.gf("user-hooks", "install", "--settings", str(self.settings)).returncode, 0)
        result = self.gf("user-hooks", "status", "--settings", str(self.settings), "--json")
        self.assertEqual(result.returncode, 1)
        status = json.loads(result.stdout)
        self.assertTrue(status["installed"])
        self.assertTrue(status["disabled"])
        human = self.gf("user-hooks", "status", "--settings", str(self.settings))
        self.assertIn("disableAllHooks", human.stdout)

    def test_status_with_nothing_installed_does_not_claim_sessions_are_blocked(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{}\n")
        old = self.shim("old", "exit 2", "slopbrake-hook")
        path = f"{old}{os.pathsep}{os.environ['PATH']}"
        result = self.gf("user-hooks", "status", "--settings", str(self.settings), PATH=path)
        self.assertEqual(result.returncode, 1, result.stdout)  # a health check; B18 fixed only the wording
        self.assertNotIn("block every session", result.stdout)
        self.assertIn("none", result.stdout)
        self.assertIn("install refuses until", result.stdout)
        self.assertEqual(self.gf("user-hooks", "install", "--settings", str(self.settings)).returncode, 0)
        result = self.gf("user-hooks", "status", "--settings", str(self.settings), PATH=path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("block every session", result.stdout)


if __name__ == "__main__":
    unittest.main()
