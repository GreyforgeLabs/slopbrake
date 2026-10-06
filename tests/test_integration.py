"""Behaviour that spans kit areas: the installed kit is never treated as the repo's own code."""
import json
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
        sh(["git", "update-ref", "refs/slopbrake/last-green/main", "HEAD"], self.root)  # B2: init's ratchet
        self.assertEqual(self.stop().returncode, 0)


class StatusSeesTheGate(Scratch):
    def test_a_scripts_check_without_every_kit_stage_is_a_gap(self):
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("pyproject.toml", "[project]\nname = 'shop'\n")
        self.commit("init")
        self.assertEqual(self.slopbrake("init", ".").returncode, 0)
        check = self.root / "scripts/check"
        check.write_text(check.read_text().replace("STAGES=(lint types boundaries tests smoke", "STAGES=(lint types boundaries tests"))
        result = self.slopbrake("status", ".")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("scripts/check does not run the kit's smoke stage", result.stdout)


class UserLevelGuard(Scratch):
    def test_block_message_names_the_guard_and_the_way_forward(self):
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write(".claude/slopbrake.json", '{"stack": "python"}\n')
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"command": "git reset --hard"}})
        result = subprocess.run([sys.executable, "-m", "slopbrake", "hook", "pre-tool-use"], input=payload, cwd=self.root,
                                capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(HOME)), check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED by the destructive-git guard (H5)", result.stderr)
        self.assertIn("ask them to run it", result.stderr)


class KitWritesNoBytecode(Scratch):
    def test_no_kit_script_leaves_a_pycache_in_the_repo(self):
        # The kit lives in scripts/slopbrake/, a one-way path: bytecode written there by a gate run
        # dirties the tree and trips the default-branch door on the next commit.
        sh(["git", "init", "-q", "-b", "main"], self.root)
        kit = self.root / "scripts/slopbrake"
        kit.mkdir(parents=True)
        for script in GUARDS.glob("*.py"):
            (kit / script.name).write_text(script.read_text())
        self.write("tests/test_ok.py", "def test_ok():\n    assert 1 + 1 == 2\n")
        self.commit("init")
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        for args in (["changed_ranges.py"], ["tautology_py.py", "tests"], ["boundaries_py.py", "."],
                     ["door_classify.py", "--rules", str(HOME / "slopbrake/kit/common/.claude/door-rules.yml"), "--base", "HEAD"],
                     ["mutation_py.py", "--base", "HEAD", "--test-cmd", "true"]):
            sh([sys.executable, str(kit / args[0]), *args[1:]], self.root, env)
        self.assertEqual(sorted(p.name for p in kit.rglob("*.pyc")), [])


class StatusSeesTrackedBytecode(Scratch):
    def test_tracked_kit_bytecode_is_a_gap(self):
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("pyproject.toml", "[project]\nname = 'shop'\n")
        self.commit("init")
        self.assertEqual(self.slopbrake("init", ".").returncode, 0)
        self.write("scripts/slopbrake/__pycache__/common.cpython-314.pyc", "x")
        sh(["git", "add", "-f", "scripts/slopbrake/__pycache__/common.cpython-314.pyc"], self.root)
        result = self.slopbrake("status", ".")
        self.assertIn("kit bytecode is tracked in git", result.stdout)


class IsolatedFromTheOperator(unittest.TestCase):
    def test_the_suite_never_writes_the_operators_trust_store(self):
        # init trusts its repo; a suite run against the real ~/.config would fill the operator's trust
        # store with temp repos. scripts/check points XDG_CONFIG_HOME/XDG_STATE_HOME at a scratch dir.
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").resolve()
        self.assertNotEqual(config, (Path.home() / ".config").resolve(),
                            "run the tests through scripts/check (or set XDG_CONFIG_HOME to a scratch dir)")


class InitInALinkedWorktree(Scratch):
    def test_init_in_a_linked_worktree_leaves_the_shared_hooks_setting_alone(self):
        # A staged branch is built in a linked worktree; core.hooksPath lives in the shared config, so setting
        # it there would switch hooks for the main checkout before anyone merged the kit.
        main = self.root / "main"
        main.mkdir()
        sh(["git", "init", "-q", "-b", "main"], main)
        (main / "pyproject.toml").write_text("[project]\nname = 'shop'\n")
        sh(["git", "add", "-A"], main)
        sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", "init"], main)
        stage = self.root / "stage"
        sh(["git", "worktree", "add", "-q", "-b", "slopbrake", str(stage)], main)
        env = dict(os.environ, PYTHONPATH=str(HOME))
        result = sh([sys.executable, "-m", "slopbrake", "init", str(stage)], self.root, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sh(["git", "config", "--get", "core.hooksPath"], main).stdout.strip(), "")
        self.assertIn("linked worktree", result.stdout)
        self.assertEqual((stage / "CLAUDE.md").read_text().splitlines()[0], "# main")

    def test_init_no_trust_leaves_the_trust_store_alone(self):
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("pyproject.toml", "[project]\nname = 'shop'\n")
        self.commit("init")
        config = self.root / "xdg"
        env = dict(os.environ, PYTHONPATH=str(HOME), XDG_CONFIG_HOME=str(config))
        result = sh([sys.executable, "-m", "slopbrake", "init", ".", "--no-trust"], self.root, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((config / "slopbrake/trusted.json").exists())


class WorktreeGateTestsItsOwnTree(Scratch):
    """A staged branch lives in a linked worktree and borrows the main checkout's .venv, whose editable
    install points at the main tree: the tests stage must import the worktree's code and see the venv's tools."""

    def setUp(self):
        super().setUp()
        self.main = self.root / "main"
        (self.main / "src/pkg").mkdir(parents=True)
        (self.main / "src/pkg/__init__.py").write_text("VALUE = 'main'\n")
        sh(["git", "init", "-q", "-b", "main"], self.main)
        sh(["git", "add", "-A"], self.main)
        sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", "init"], self.main)
        venv = self.main / ".venv/bin"
        venv.mkdir(parents=True)
        # Like an editable install: the main tree's src is appended after PYTHONPATH.
        (venv / "python").write_text(f'#!/bin/sh\nexport PYTHONPATH="${{PYTHONPATH:+$PYTHONPATH:}}{self.main}/src"\n'
                                     f'exec {sys.executable} "$@"\n')
        (venv / "venvtool").write_text("#!/bin/sh\nexit 0\n")
        for f in venv.iterdir():
            f.chmod(0o755)
        self.wt = self.root / "wt"
        sh(["git", "worktree", "add", "-q", "-b", "feat", str(self.wt)], self.main)
        (self.wt / "src/pkg/__init__.py").write_text("VALUE = 'wt'\n")

    def gate(self, test_args):
        template = (HOME / "slopbrake/kit/python/scripts/check").read_text()
        for key, value in (("LINT_CMD", "true"), ("TYPES_CMD", "true"), ("TEST_ARGS", test_args)):
            template = template.replace(f"@{key}@", value)
        kit = self.wt / "scripts/slopbrake"
        kit.mkdir(parents=True)
        for f in GUARDS.iterdir():
            if f.is_file():
                (kit / f.name).write_text(f.read_text())
        check = self.wt / "scripts/check"
        check.write_text(template)
        check.chmod(0o755)
        return sh([str(check), "tests"], self.wt)

    def test_the_worktrees_own_code_is_imported(self):
        (self.wt / "probe.py").write_text("import sys, pkg\nsys.exit(0 if pkg.VALUE == 'wt' else 1)\n")
        result = self.gate("probe.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_venvs_tools_are_on_path(self):
        (self.wt / "probe.py").write_text("import shutil, sys\nsys.exit(0 if shutil.which('venvtool') else 1)\n")
        result = self.gate("probe.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class StatusTsCoverage(Scratch):
    def setUp(self):
        super().setUp()
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("package.json", '{"name": "t", "scripts": {"typecheck": "tsc"}}\n')
        self.write("tsconfig.json", "{}\n")
        self.write("src/app.ts", "export const a = 1;\n")
        self.commit("init")
        env = dict(os.environ, PYTHONPATH=str(HOME), XDG_CONFIG_HOME=str(self.root / "xdg"))
        self.assertEqual(sh([sys.executable, "-m", "slopbrake", "init", ".", "--stack", "typescript"], self.root, env).returncode, 0)
        self.env = env

    def status(self):
        return sh([sys.executable, "-m", "slopbrake", "status", "."], self.root, self.env).stdout

    def test_untracked_scratch_is_not_a_coverage_gap(self):
        self.write("scratch/tool.ts", "export const t = 1;\n")
        self.assertNotIn("outside every scripts/check glob", self.status())

    def test_tracked_sources_outside_the_globs_are_a_gap_unless_declared_uncovered(self):
        self.write("vendor/lib/x.ts", "export const x = 1;\n")
        self.commit("vendored")
        self.assertIn("vendor/lib/", self.status())
        check = self.root / "scripts/check"
        check.write_text(check.read_text().replace("STAGES=(", "UNCOVERED=(vendor/lib)  # vendored upstream code\nSTAGES=(", 1))
        self.assertNotIn("outside every scripts/check glob", self.status())


class DefaultBranchDoorIgnoresUntracked(Scratch):
    def setUp(self):
        super().setUp()
        sh(["git", "init", "-q", "-b", "main"], self.root)
        kit = self.root / "scripts/slopbrake"
        kit.mkdir(parents=True)
        for f in GUARDS.glob("*.py"):
            (kit / f.name).write_text(f.read_text())
        (self.root / ".claude").mkdir()
        (self.root / ".claude/door-rules.yml").write_text((HOME / "slopbrake/kit/common/.claude/door-rules.yml").read_text())
        self.write("app.py", "x = 1\n")
        self.commit("init")

    def pr(self):
        env = {k: v for k, v in os.environ.items() if k not in ("GIT_INDEX_FILE", "PR_BODY_FILE", "GITHUB_EVENT_PATH")}
        return sh([sys.executable, "scripts/slopbrake/pr_body_check.py"], self.root, env)

    def test_untracked_scratch_on_main_is_not_a_one_way_commit(self):
        # A permanent untracked folder (release scratch with rm -rf in its scripts) is not about to be committed.
        self.write("release-prep/render.sh", 'rm -rf "$work"\n')
        result = self.pr()
        self.assertEqual(result.returncode, 78, result.stdout)

    def test_a_staged_one_way_file_on_main_still_blocks(self):
        self.write("release-prep/render.sh", 'rm -rf "$work"\n')
        sh(["git", "add", "release-prep/render.sh"], self.root)
        self.assertEqual(self.pr().returncode, 1)

    def test_a_tracked_one_way_edit_on_main_still_blocks(self):
        self.write("app.py", "import shutil\nshutil.rmtree(path)\n")
        self.assertEqual(self.pr().returncode, 1)


if __name__ == "__main__":
    unittest.main()
