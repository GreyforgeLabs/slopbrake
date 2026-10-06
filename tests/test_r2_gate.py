"""Round 2 gate fixes: ratchet ref encoding (B1), base on the default branch (B2), Stop hook and skip
directories (B3), honest git hooks (B4), tests-only changes (B5), run-time interpreter (B6), no-mutate (B14)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
KIT = HOME / "slopbrake/kit"
GUARDS = KIT / "common/scripts/slopbrake"
HOOKS = KIT / "common/.claude/hooks"
GITHOOKS = KIT / "common/.githooks"

GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]
OVERRIDES = ("SLOPBRAKE_BASE", "GITHUB_BASE_REF", "SLOPBRAKE_NO_RECORD", "MUTATION_FLOOR", "MUTATION_MAX",
             "MUTATION_TEST_CMD", "TEST_CMD", "CLAUDE_PROJECT_DIR")
CLEAN_ENV = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
SKIP = 78
GREEN = "refs/slopbrake/last-green/"
ZERO = "0" * 40
TEST_HEADER = "import unittest\nfrom shop import {names}\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n"


def sh(cmd, cwd, env=None, stdin=None, **extra):
    env = dict(env if env is not None else CLEAN_ENV, **extra)
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False)


def script(name, *args, cwd, **env):
    return sh([sys.executable, str(GUARDS / name), *args], cwd, **env)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.aux = Path(self.tmp.name) / "aux"
        self.root.mkdir()
        self.aux.mkdir()

    def write(self, rel, text, root=None):
        path = (root or self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def executable(self, rel, text, root=None):
        path = self.write(rel, text, root)
        path.chmod(0o755)
        return path

    def git(self, *args, cwd=None):
        result = sh(["git", *GIT_ID, *args], cwd or self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def git_repo(self, branch="main"):
        self.git("init", "-q", "-b", branch)
        self.write("README.md", "# demo\n")
        self.commit("init")

    def commit(self, message, cwd=None):
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "-q", "--no-verify", "-m", message, cwd=cwd)

    def ref(self, name):
        return sh(["git", "rev-parse", "-q", "--verify", name], self.root).stdout.strip()

    def base(self, **env):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import common; "
                "print(common.base_ref()); print(common.describe_base())")
        result = sh([sys.executable, "-c", code, str(GUARDS)], self.root, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def empty_tree(self):
        return self.git("hash-object", "-t", "tree", os.devnull)

    def seed(self, source, body, names, root=None):
        self.write("shop.py", source, root)
        self.write("tests/test_shop.py", TEST_HEADER.format(names=names) + textwrap.indent(textwrap.dedent(body), " " * 8),
                   root)

    def floor(self, *args, **env):
        return script("mutation_py.py", *args, cwd=self.root, **env)


# ── B1: one ref per branch, "/" encoded as %2F ───────────────────────────────

class RatchetEncoding(Scratch):
    CHECK = "#!/usr/bin/env bash\ncd \"$(dirname \"$0\")/..\"\nsource scripts/slopbrake/run-stages.sh\n" \
            "STAGES=(a)\nstage_a() { :; }\nrun_stages \"$@\"\n"

    def setUp(self):
        super().setUp()
        self.git_repo()
        self.write("scripts/slopbrake/run-stages.sh", (GUARDS / "run-stages.sh").read_text())
        self.executable("scripts/check", self.CHECK)
        self.commit("gate")

    def test_feature_and_feature_slash_x_both_record(self):
        for branch in ("feature", "feature/x"):  # feature is deleted first: the ratchet stays behind
            self.git("checkout", "-q", "main")
            self.git("branch", "-q", "-D", "feature") if branch == "feature/x" else None
            self.git("checkout", "-q", "-b", branch)
            result = sh(["scripts/check"], self.root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("fatal", result.stderr)
        refs = self.git("for-each-ref", "--format=%(refname)", "refs/slopbrake/").split()
        self.assertEqual(refs, [GREEN + "feature", GREEN + "feature%2Fx"])

    def test_common_names_the_same_ref(self):
        code = "import sys; sys.path.insert(0, sys.argv[1]); import common; print(common.last_green_ref('feature/x'))"
        result = sh([sys.executable, "-c", code, str(GUARDS)], self.root)
        self.assertEqual(result.stdout.strip(), GREEN + "feature%2Fx", result.stderr)

    def test_a_slashed_branch_uses_its_encoded_ratchet_as_the_base(self):
        self.git("checkout", "-q", "-b", "feature/x")
        self.git("update-ref", GREEN + "feature%2Fx", "HEAD")
        self.git("update-ref", GREEN + "feature", "HEAD")
        self.assertEqual(self.base()[0], GREEN + "feature%2Fx")

    def test_the_stop_hook_reads_the_encoded_ratchet(self):
        self.git("checkout", "-q", "-b", "feature/x")
        self.write("NOTES.md", "x\n")
        self.commit("doc")
        log = self.aux / "log"
        self.executable("scripts/check", f"#!/usr/bin/env bash\necho \"args=$*\" >> {str(log)!r}\n")
        self.commit("logging check")
        self.git("update-ref", GREEN + "feature%2Fx", "HEAD~1")
        result = sh([str(HOOKS / "require-green.sh")], self.root, stdin='{"session_id": "b1"}',
                    CLAUDE_PROJECT_DIR=str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().splitlines(), ["args="])
        log.unlink()
        self.git("update-ref", GREEN + "feature%2Fx", "HEAD")
        sh([str(HOOKS / "require-green.sh")], self.root, stdin='{"session_id": "b1"}', CLAUDE_PROJECT_DIR=str(self.root))
        self.assertFalse(log.exists())


# ── B2: base on the default branch ───────────────────────────────────────────

class DefaultBranchBase(Scratch):
    UNTESTED = ("def tax(x):\n    return x * 2 + 1\n", "tax(500)\n", "tax")

    def test_no_ratchet_measures_from_the_empty_tree(self):
        self.git_repo()
        self.seed(*self.UNTESTED)
        self.commit("untested work on main, no ratchet (init before the first commit)")
        base, described = self.base()
        self.assertEqual(base, self.empty_tree())
        self.assertIn("empty tree", described)
        result = self.floor()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("below floor", result.stdout)

    def test_a_ratchet_that_is_no_longer_an_ancestor_measures_from_the_merge_base(self):
        self.git_repo()
        kept = self.git("rev-parse", "HEAD")
        self.write("NOTES.md", "x\n")
        self.commit("doc")
        self.git("update-ref", GREEN + "main", "HEAD")
        self.seed(*self.UNTESTED)
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "--amend", "--no-edit")
        base, described = self.base()
        self.assertEqual(base, kept)
        self.assertIn("not an ancestor", described)
        self.assertIn("below floor", self.floor().stdout)

    def test_a_repo_with_commits_and_no_base_ref_is_never_skipped(self):
        self.git_repo(branch="trunk")
        self.seed(*self.UNTESTED)
        self.commit("work on trunk")
        result = self.floor()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("below floor", result.stdout)

    def test_everything_pushed_with_no_ratchet_is_the_remote_base(self):
        """A fresh clone or CI on main: the remote already has (and gated) every commit."""
        self.git_repo()
        self.git("init", "-q", "--bare", str(self.aux / "remote.git"))
        self.git("remote", "add", "origin", str(self.aux / "remote.git"))
        self.git("push", "-q", "-u", "origin", "main")
        self.assertEqual(self.base()[0], "origin/main")

    def test_a_feature_branch_at_main_without_a_ratchet_is_measured_against_main(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feat")
        self.assertEqual(self.base()[0], "main")


# ── B3: the Stop hook ────────────────────────────────────────────────────────

class StopHook(Scratch):
    def setUp(self):
        super().setUp()
        self.git_repo()
        self.log = self.aux / "check.log"
        self.executable("scripts/check", f"""\
            #!/usr/bin/env bash
            cd "$(dirname "$0")/.."
            echo "args=$* max=${{MUTATION_MAX-unset}}" >> {str(self.log)!r}
            """)
        self.commit("gate")

    def stop(self):
        result = sh([str(HOOKS / "require-green.sh")], self.root, stdin=json.dumps({"session_id": "s"}),
                    CLAUDE_PROJECT_DIR=str(self.root))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return result, logged

    def test_dirty_tree_and_unverified_commits_get_the_full_gate(self):
        self.git("update-ref", GREEN + "main", "HEAD~1")
        self.write("run.log", "scratch\n")
        result, logged = self.stop()
        self.assertEqual(logged, ["args= max=40"], result.stderr)

    def test_dirty_only_gets_the_fast_gate(self):
        self.git("update-ref", GREEN + "main", "HEAD")
        self.write("calc.py", "x = 1\n")
        self.assertEqual(self.stop()[1], ["args=--fast max=unset"])

    def test_no_ratchet_on_the_default_branch_is_unverified(self):
        self.assertEqual(self.stop()[1], ["args= max=40"])

    def test_a_ratchet_that_is_no_longer_an_ancestor_is_unverified(self):
        self.git("update-ref", GREEN + "main", "HEAD")
        self.write("calc.py", "x = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "--amend", "--no-edit")
        self.assertEqual(self.stop()[1], ["args= max=40"])

    def test_commits_on_a_detached_head_get_the_full_gate(self):
        self.git("update-ref", GREEN + "main", "HEAD")
        self.git("checkout", "-q", "--detach")
        self.write("calc.py", "x = 1\n")
        self.commit("detached work")
        result, logged = self.stop()
        self.assertEqual(logged, ["args= max=40"], result.stderr)

    def test_a_nested_worktree_is_not_a_code_change(self):
        self.git("update-ref", GREEN + "main", "HEAD")
        self.git("worktree", "add", "-q", "-b", "wip", str(self.root / ".claude/worktrees/w1"))
        self.write(".claude/worktrees/w1/calc.py", "x = 1\n")
        self.assertEqual(self.stop()[1], [])


class SkipDirectories(Scratch):
    TAUTOLOGY = "def test_t():\n    items = [1]\n    assert sum(items) == sum(items)\n"

    def test_test_quality_skips_dot_claude_and_nested_worktrees(self):
        self.git_repo()
        self.write(".claude/skills/x/tests/test_a.py", self.TAUTOLOGY)
        self.write("vendor/other/.git", "gitdir: /elsewhere\n")
        self.write("vendor/other/tests/test_b.py", self.TAUTOLOGY)
        self.write("tests/test_ok.py", "def test_t():\n    assert 1 + 1 == 2\n")
        result = script("tautology_py.py", ".", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.write("tests/test_bad.py", self.TAUTOLOGY)
        self.assertEqual(script("tautology_py.py", ".", cwd=self.root).returncode, 1)

    def test_boundaries_skip_dot_claude_and_nested_worktrees(self):
        self.write("myapp/__init__.py", "")
        self.write("myapp/core/__init__.py", "")
        self.write("myapp/core/_engine.py", "def go():\n    pass\n")
        bad = "from myapp.core._engine import go\n"
        self.write(".claude/worktrees/w1/.git", "gitdir: /elsewhere\n")
        self.write(".claude/worktrees/w1/myapp/ui.py", bad)
        self.write("wt2/.git", "gitdir: /elsewhere\n")
        self.write("wt2/myapp/ui.py", bad)
        result = script("boundaries_py.py", ".", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.write("myapp/ui.py", bad)
        self.assertEqual(script("boundaries_py.py", ".", cwd=self.root).returncode, 1)


# ── B4: honest pre-commit and pre-push ───────────────────────────────────────

class PreCommit(Scratch):
    def test_staged_files_with_unstaged_changes_block_the_commit(self):
        self.git_repo()
        self.executable("scripts/check", "#!/usr/bin/env bash\nexit 0\n")
        hook = self.root / ".githooks/pre-commit"
        hook.parent.mkdir()
        shutil.copy(GITHOOKS / "pre-commit", hook)
        self.git("config", "core.hooksPath", ".githooks")
        self.commit("gate")
        self.write("calc.py", "x = 1\n")
        self.git("add", "calc.py")
        self.write("calc.py", "x = 2\n")
        result = sh(["git", *GIT_ID, "commit", "-q", "-m", "partial"], self.root)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("calc.py", result.stderr)
        self.assertIn("stage them or stash them", result.stderr)
        self.assertIn("--no-verify", result.stderr)
        self.git("add", "calc.py")
        self.assertEqual(sh(["git", *GIT_ID, "commit", "-q", "-m", "whole"], self.root).returncode, 0)


class PrePush(Scratch):
    def setUp(self):
        super().setUp()
        self.git_repo()
        self.log = self.aux / "check.log"
        self.executable("scripts/check", f"#!/usr/bin/env bash\necho \"base=${{SLOPBRAKE_BASE-unset}}\" >> {str(self.log)!r}\n")
        self.commit("gate")
        self.head = self.git("rev-parse", "HEAD")

    def push(self, *lines):
        result = sh([str(GITHOOKS / "pre-push"), "origin", "url"], self.root, stdin="".join(l + "\n" for l in lines))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return result, logged

    def test_a_dirty_tracked_tree_blocks_the_push(self):
        self.write("README.md", "# changed, not committed\n")
        result, logged = self.push(f"refs/heads/main {self.head} refs/heads/main {ZERO}")
        self.assertEqual((result.returncode, logged), (1, []), result.stderr)
        self.assertIn("push from a clean tree", result.stderr)
        self.assertIn("README.md", result.stderr)

    def test_untracked_files_do_not_block(self):
        self.write("scratch.txt", "x\n")
        self.assertEqual(self.push(f"refs/heads/main {self.head} refs/heads/main {ZERO}")[0].returncode, 0)

    def test_a_ref_that_is_not_head_says_check_it_out(self):
        self.git("branch", "old", "HEAD~1")
        result, logged = self.push(f"refs/heads/old {self.git('rev-parse', 'old')} refs/heads/old {ZERO}")
        self.assertEqual((result.returncode, logged), (1, []), result.stderr)
        self.assertIn("check out refs/heads/old and push from a clean tree", result.stderr)

    def test_a_ref_checked_out_in_another_worktree_says_push_it_from_there(self):
        wt = self.aux / "wt-y"
        self.git("worktree", "add", "-q", "-b", "feature-y", str(wt), "HEAD~1")
        self.write("y.txt", "y\n", root=wt)
        self.commit("y", cwd=wt)
        result, _ = self.push(f"refs/heads/feature-y {self.git('rev-parse', 'feature-y')} refs/heads/feature-y {ZERO}")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"push it from {wt}", result.stderr)

    def test_a_new_branch_is_based_on_what_the_remote_has(self):
        self.write("NOTES.md", "x\n")
        self.commit("third")
        line = f"refs/heads/feat {self.git('rev-parse', 'HEAD')} refs/heads/feat {ZERO}"
        self.assertEqual(self.push(line)[1], [f"base={self.empty_tree()}"])
        oldest, older = self.git("rev-parse", "HEAD~2"), self.git("rev-parse", "HEAD~1")
        self.git("update-ref", "refs/remotes/origin/master", oldest)
        self.assertEqual(self.push(line)[1], [f"base={oldest}"])
        self.git("update-ref", "refs/remotes/origin/main", older)
        self.assertEqual(self.push(line)[1], [f"base={older}"])
        self.git("update-ref", "refs/remotes/origin/trunk", oldest)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        self.assertEqual(self.push(line)[1], [f"base={oldest}"])

    def test_a_new_branch_from_unpushed_main_commits_is_mutation_checked(self):
        """Committed on main, then branched to open a PR: the remote never saw those commits."""
        kit = self.root / "scripts/slopbrake"
        shutil.copytree(GUARDS, kit)
        template = (KIT / "python/scripts/check").read_text()
        for key, value in (("LINT_CMD", "true"), ("TYPES_CMD", "true"), ("TEST_ARGS", "-m unittest discover -s tests")):
            template = template.replace(f"@{key}@", value)
        self.executable("scripts/check", template)
        self.commit("real gate")
        self.git("init", "-q", "--bare", str(self.aux / "remote.git"))
        self.git("remote", "add", "origin", str(self.aux / "remote.git"))
        self.git("push", "-q", "--no-verify", "-u", "origin", "main")
        self.seed(*DefaultBranchBase.UNTESTED)
        self.commit("untested, on main")
        self.git("switch", "-q", "-c", "feature")
        head = self.git("rev-parse", "HEAD")
        result = sh([str(GITHOOKS / "pre-push"), "origin", "url"], self.root,
                    stdin=f"refs/heads/feature {head} refs/heads/feature {ZERO}\n")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("below floor", result.stdout)


# ── B5: tests-only changes mutate the modules those tests import ─────────────

class TestsOnlyChanges(Scratch):
    def setUp(self):
        super().setUp()
        self.git_repo()
        self.write("calc/__init__.py", "")
        self.write("calc/core.py", "def one():\n    return 1\n\n\ndef two():\n    return one() + 1\n")
        self.write("tests/test_core.py", "import unittest\nfrom calc.core import one, two\n\n\n"
                                         "class T(unittest.TestCase):\n    def test_it(self):\n"
                                         "        self.assertEqual(one(), 1)\n        self.assertEqual(two(), 2)\n")
        self.commit("tested")
        self.git("checkout", "-q", "-b", "feat")

    def test_weakened_assertions_are_mutation_checked(self):
        self.write("tests/test_core.py", "import unittest\nfrom calc.core import one, two\n\n\n"
                                         "class T(unittest.TestCase):\n    def test_it(self):\n"
                                         "        self.assertIsNotNone(one())\n        self.assertIsNotNone(two())\n")
        self.commit("weaken")
        result = self.floor()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("only tests changed", result.stdout)
        self.assertIn("calc/core.py", result.stdout)
        self.assertIn("below floor", result.stdout)

    def test_a_deleted_assertion_is_mutation_checked(self):
        self.write("tests/test_core.py", "import unittest\nfrom calc.core import one, two\n\n\n"
                                         "class T(unittest.TestCase):\n    def test_it(self):\n"
                                         "        self.assertEqual(one(), 1)\n        two()\n")
        self.commit("drop an assertion")
        result = self.floor()
        self.assertIn("only tests changed", result.stdout)
        self.assertIn("below floor", result.stdout)

    def test_a_deleted_assertion_line_is_still_a_changed_test(self):
        self.write("tests/test_core.py", "import unittest\nfrom calc.core import one, two\n\n\n"
                                         "class T(unittest.TestCase):\n    def test_it(self):\n"
                                         "        self.assertEqual(one(), 1)\n")
        self.commit("delete an assertion line")
        result = self.floor()
        self.assertIn("only tests changed", result.stdout)
        self.assertIn("below floor", result.stdout)

    def test_only_the_functions_the_changed_tests_call_are_mutated(self):
        # A big module the changed test merely imports must not be mutated whole (it made the gate unbounded).
        self.write("calc/big.py", "".join(f"def g{i}(x):\n    return x * {i}\n\n\n" for i in range(80)))
        self.write("tests/test_big.py", "import unittest\nfrom calc import big\nfrom calc.core import one\n\n\n"
                                        "class B(unittest.TestCase):\n    def test_b(self):\n        self.assertEqual(one(), 1)\n")
        self.commit("big module")
        self.git("update-ref", "refs/heads/main", "HEAD")
        self.write("tests/test_big.py", (self.root / "tests/test_big.py").read_text().replace(
            "self.assertEqual(one(), 1)", "self.assertIsNotNone(one())"))
        result = self.floor()
        self.assertIn("mutating what the changed tests call: calc/core.py (one)", result.stdout)
        self.assertNotIn("calc/big.py", result.stdout)

    def test_a_test_edit_that_calls_nothing_first_party_is_a_skip(self):
        self.write("tests/test_core.py", (self.root / "tests/test_core.py").read_text()
                   + "\n\nALLOWED = ['check', 'setup.sh']\n")
        self.commit("allowlist")
        result = self.floor()
        self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
        self.assertIn("call no first-party function", result.stdout)

    def test_strong_tests_still_pass_and_mutants_are_capped(self):
        body = "".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(60))
        self.write("calc/many.py", body)
        asserts = "".join(f"        self.assertEqual(many.f{i}(1), {i + 1})\n" for i in range(60))
        self.write("tests/test_core.py", "import unittest\nfrom calc import many\nfrom calc.core import one\n\n\n"
                                         "class T(unittest.TestCase):\n    def test_it(self):\n" + asserts
                                         + "        self.assertEqual(one(), 1)\n")
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "-m", "more tests")
        self.git("update-ref", "refs/heads/main", "HEAD~0")  # calc/many.py is on the base: only tests change
        self.write("tests/test_core.py", (self.root / "tests/test_core.py").read_text() + "\n")
        self.write("tests/test_extra.py", "import unittest\nfrom calc import many\n\n\n"
                                          "class E(unittest.TestCase):\n    def test_e(self):\n"
                                          "        self.assertEqual(many.f3(2), 5)\n")
        result = self.floor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"mutation: (\d+)/(\d+) mutants killed")
        self.assertIn("calc/many.py (f3)", result.stdout)


# ── B6: the interpreter is resolved at run time ──────────────────────────────

class RuntimeInterpreter(Scratch):
    def render(self, root):
        template = (KIT / "python/scripts/check").read_text()
        for key, value in (("LINT_CMD", "true"), ("TYPES_CMD", "true"), ("TEST_ARGS", "-m unittest discover -s tests")):
            template = template.replace(f"@{key}@", value)
        shutil.copytree(GUARDS, root / "scripts/slopbrake")
        self.executable("scripts/check", template, root)

    def fake_venv(self, root, marker):
        self.executable(".venv/bin/python", f'#!/bin/sh\necho "$0" >> {str(marker)!r}\nexec {sys.executable} "$@"\n', root)

    def test_main_worktree_venv_is_used_from_a_linked_worktree_and_spaces_survive(self):
        self.root = Path(self.tmp.name) / "my repo"
        self.root.mkdir()
        self.git_repo()
        self.write(".gitignore", ".venv/\n")
        self.render(self.root)
        self.seed("def tax(x):\n    return x * 2\n", "self.assertEqual(tax(2), 4)\n", "tax")
        self.commit("gate")
        marker = self.aux / "marker"
        self.fake_venv(self.root, marker)
        result = sh(["scripts/check", "tests"], self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(marker.read_text().split("\n")[0], f"{self.root}/.venv/bin/python")
        marker.unlink()
        wt = Path(self.tmp.name) / "linked wt"
        self.git("worktree", "add", "-q", "-b", "wt", str(wt))
        self.write("shop.py", "def tax(x):\n    return x * 2 + 0\n", root=wt)
        result = sh(["scripts/check", "tests", "mutation"], wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"{self.root}/.venv/bin/python", marker.read_text())

    def test_without_a_venv_it_is_python3(self):
        self.git_repo()
        self.render(self.root)
        self.write("tests/test_t.py", "import unittest\n\n\nclass T(unittest.TestCase):\n"
                                      "    def test_t(self):\n        self.assertEqual(1, 1)\n")
        self.commit("gate")
        result = sh(["scripts/check", "tests"], self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# ── B14: no-mutate cannot clear the floor alone ──────────────────────────────

class NoMutate(Scratch):
    def test_every_changed_line_marked_no_mutate_fails(self):
        self.git_repo()
        self.seed("def disc(x):  # slopbrake: no-mutate\n    return x - 10  # slopbrake: no-mutate\n",
                  "disc(150)\n", "disc")
        result = self.floor("--base", "main")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("every changed line is marked no-mutate; mark only equivalent mutants", result.stdout)
        self.assertIn("2 changed lines excluded by no-mutate", result.stdout)

    def test_the_count_is_printed_when_nothing_is_excluded(self):
        self.git_repo()
        self.seed("def tax(x):\n    return x * 2\n", "self.assertEqual(tax(2), 4)\n", "tax")
        result = self.floor("--base", "main")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("0 changed lines excluded by no-mutate", result.stdout)

    def test_the_pragma_must_end_the_line(self):
        self.git_repo()
        self.seed("def tax(x):\n    return x * 2  # slopbrake: no-mutate? no: tested below\n",
                  "tax(1)\n", "tax")
        self.assertIn("0 changed lines excluded", self.floor("--base", "main").stdout)


# ── Round 2 review: callers of an empty-tree base, deleted tests, wording ────

class EmptyTreeBaseCallers(Scratch):
    """B2 lets base_ref return the empty tree on main with no ratchet; every caller must take it."""

    def setUp(self):
        super().setUp()
        self.git_repo()
        self.write(".claude/door-rules.yml", (KIT / "common/.claude/door-rules.yml").read_text())
        self.write("f", "x\n")
        self.commit("review: tighten the parser")

    def test_door_classify_measures_from_the_empty_tree(self):
        result = script("door_classify.py", "--json", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("measuring nothing", result.stderr)
        self.assertEqual(json.loads(result.stdout)["base"], self.empty_tree())
        self.assertEqual(json.loads(result.stdout)["files"], 3)

    def test_pr_body_check_logs_reviews_since_the_empty_tree(self):
        self.write("body.md", "## Summary\n\nx\n\n## Review log\n\nnothing yet\n")
        result = script("pr_body_check.py", "--body-file", "body.md", cwd=self.root)
        self.assertNotIn("failed", result.stdout + result.stderr)
        self.assertIn("Review log is missing review: commit 'review: tighten the parser' (C3)", result.stdout)


class DeletedTestFile(Scratch):
    def test_deleting_a_modules_only_test_file_is_mutation_checked(self):
        self.git_repo()
        self.write("shop/__init__.py", "def one():\n    return 1\n")
        self.write("tests/test_one.py", "import unittest\nfrom shop import one\n\n\n"
                                        "class T(unittest.TestCase):\n    def test_one(self):\n"
                                        "        self.assertEqual(one(), 1)\n")
        self.commit("tested")
        self.git("checkout", "-q", "-b", "feat")
        self.git("rm", "-q", "tests/test_one.py")
        self.write("tests/test_x.py", "import unittest\n\n\nclass X(unittest.TestCase):\n"
                                      "    def test_m(self):\n        pass\n")
        self.commit("drop the test")
        result = self.floor()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("only tests changed", result.stdout)
        self.assertIn("shop/__init__.py", result.stdout)
        self.assertIn("below floor", result.stdout)


class ReviewWording(Scratch):
    def test_a_merge_base_that_is_the_empty_tree_is_named(self):
        self.git_repo()
        self.git("update-ref", GREEN + "main", "HEAD")
        self.seed("def tax(x):\n    return x * 2\n", "tax(1)\n", "tax")
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "--amend", "--no-edit")
        base, described = self.base()
        self.assertEqual(base, self.empty_tree())
        self.assertNotIn("?", described)
        self.assertIn("the empty tree", described)
        self.assertIn("not an ancestor", described)

    def test_the_test_command_is_printed_as_it_runs(self):
        self.git_repo()
        self.seed("def tax(x):\n    return x * 2\n", "self.assertEqual(tax(2), 4)\n", "tax")
        result = self.floor("--base", "main")
        self.assertIn("test command: python3 -m unittest discover -s tests\n", result.stdout)

    def test_the_no_mutate_count_is_printed_when_only_tests_changed(self):
        self.git_repo()
        self.seed("def tax(x):\n    return x * 2\n", "self.assertEqual(tax(2), 4)\n", "tax")
        self.commit("tested")
        self.git("checkout", "-q", "-b", "feat")
        self.write("tests/test_shop.py", (self.root / "tests/test_shop.py").read_text() + "\n")
        result = self.floor()
        self.assertIn("only tests changed", result.stdout)
        self.assertIn("0 changed lines excluded by no-mutate", result.stdout)

    def test_the_no_mutate_count_is_printed_when_skipping(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feat")
        self.write("notes.txt", "x\n")
        result = self.floor()
        self.assertEqual(result.returncode, SKIP, result.stdout)
        self.assertIn("no changed Python source lines", result.stdout)
        self.assertIn("0 changed lines excluded by no-mutate", result.stdout)


if __name__ == "__main__":
    unittest.main()
