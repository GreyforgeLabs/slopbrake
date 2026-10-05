"""Gate core: base selection, diff collection, the stage runner, mutation, git hooks and the Stop hook."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
KIT = HOME / "slopbrake/kit"
GUARDS = KIT / "common/scripts/slopbrake"
HOOKS = KIT / "common/.claude/hooks"
GITHOOKS = KIT / "common/.githooks"
sys.path.insert(0, str(GUARDS))

from common import parse_unified_diff

GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]
OVERRIDES = ("SLOPBRAKE_BASE", "GITHUB_BASE_REF", "SLOPBRAKE_NO_RECORD", "MUTATION_FLOOR", "MUTATION_MAX",
             "MUTATION_TEST_CMD", "TEST_CMD", "CLAUDE_PROJECT_DIR")
CLEAN_ENV = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
SKIP = 78
GREEN = "refs/slopbrake/last-green/"  # + branch


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
        self.aux = Path(self.tmp.name) / "aux"  # outside the repo, so logs never dirty the tree
        self.root.mkdir()
        self.aux.mkdir()

    def write(self, rel, text, root=None):
        path = (root or self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
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

    def executable(self, rel, text, root=None):
        path = self.write(rel, text, root)
        path.chmod(0o755)
        return path

    def last_green(self, branch="main"):
        return sh(["git", "rev-parse", "-q", "--verify", GREEN + branch], self.root).stdout.strip()


class BaseSelection(Scratch):
    def base(self, *, explicit=None, **env):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import common; "
                "print(common.base_ref(sys.argv[2] or None)); print(common.describe_base(sys.argv[2] or None))")
        result = sh([sys.executable, "-c", code, str(GUARDS), explicit or ""], self.root, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_github_base_ref_beats_slopbrake_base(self):
        self.git_repo()
        self.assertEqual(self.base(GITHUB_BASE_REF="main", SLOPBRAKE_BASE="HEAD")[0], "origin/main")

    def test_upstream_beats_local_main(self):
        self.git_repo()
        self.git("init", "-q", "--bare", str(self.aux / "remote.git"))
        self.git("remote", "add", "origin", str(self.aux / "remote.git"))
        self.git("push", "-q", "-u", "origin", "main")
        self.write("shop.py", "x = 1\n")
        self.commit("unpushed")
        self.assertEqual(self.base()[0], "origin/main")

    def test_on_the_base_branch_last_green_is_the_base(self):
        self.git_repo()
        self.git("update-ref", GREEN + "main", "HEAD")
        self.write("shop.py", "x = 1\n")
        self.commit("work on main")
        base, described = self.base()
        self.assertEqual(base, GREEN + "main")
        self.assertIn("last-green", described)
        self.assertIn("main", described)

    def test_explicit_head_still_means_uncommitted_only(self):
        self.git_repo()
        self.git("update-ref", GREEN + "main", "HEAD")
        self.write("shop.py", "x = 1\n")
        self.commit("work on main")
        self.assertEqual(self.base(SLOPBRAKE_BASE="HEAD")[0], "HEAD")

    def test_a_green_run_on_another_branch_does_not_move_the_base(self):
        self.git_repo()
        self.git("update-ref", GREEN + "main", "HEAD")
        self.git("checkout", "-q", "-b", "feat")
        self.write("NOTES.md", "x\n")
        self.commit("doc")
        self.git("update-ref", GREEN + "feat", "HEAD")
        self.git("checkout", "-q", "main")
        self.write("shop.py", "x = 1\n")
        self.commit("work on main")
        self.assertEqual(self.base()[0], GREEN + "main")

    def test_detached_head_never_uses_last_green(self):
        self.git_repo()
        self.git("update-ref", GREEN + "main", "HEAD")
        self.write("shop.py", "x = 1\n")
        self.commit("work on main")
        self.git("checkout", "-q", "--detach")
        self.assertEqual(self.base()[0], "main")

    def test_no_base_falls_back_to_last_green(self):
        self.git_repo(branch="trunk")
        self.git("update-ref", GREEN + "trunk", "HEAD")
        self.write("shop.py", "x = 1\n")
        self.commit("work on trunk")
        base, described = self.base()
        self.assertEqual(base, GREEN + "trunk")
        self.assertIn("last-green", described)

    def test_no_base_is_described(self):
        self.git_repo(branch="trunk")
        base, described = self.base()
        self.assertEqual(base, "None")
        self.assertIn("no base ref", described)


class DiffCollection(Scratch):
    def ranges(self, *args, cwd=None):
        result = script("changed_ranges.py", *args, cwd=cwd or self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_diff_attributes_and_binary_content_do_not_hide_changes(self):
        self.git_repo()
        self.write(".gitattributes", "*.py -diff\n")
        self.commit("attributes")
        self.git("checkout", "-q", "-b", "feat")
        self.write("src/q.py", "import shutil\nshutil.rmtree(p)\n")
        (self.root / "src/blob.sql").write_bytes(b"\x00\x01DROP TABLE users;\n")
        self.commit("hidden")
        self.assertEqual(self.ranges("--base", "main"), "src/blob.sql:1-1,src/q.py:1-2")

    def test_committed_binary_files_do_not_crash_the_checks(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feat")
        self.write(".claude/door-rules.yml", (KIT / "common/.claude/door-rules.yml").read_text())
        (self.root / "logo.png").write_bytes(bytes(range(256)) * 3 + b"\xaa\xff\xfe")
        self.write("calc.py", "def f(x):\n    return x * 2\n")
        self.commit("png")
        self.assertEqual(self.ranges("--base", "main", "--include", "*.py"), "calc.py:1-2")
        for args in (["door_classify.py", "--json", "--base", "main"],
                     ["mutation_py.py", "--base", "main", "--test-cmd", "true"]):
            with self.subTest(args[0]):
                result = script(*args, cwd=self.root)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn(result.returncode, (0, 1), result.stderr)

    def test_only_newlines_split_diff_lines(self):
        text = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,2 @@\n"
                "+a\x0b+fake\x0cdiff --git a/y b/y\n+b\n")
        changes = parse_unified_diff(text)
        self.assertEqual(list(changes), ["x.py"])
        self.assertEqual(changes["x.py"].added, {1: "a\x0b+fake\x0cdiff --git a/y b/y", 2: "b"})

    def test_untracked_files_are_found_from_a_subdirectory(self):
        self.git_repo()
        self.write("src/x/sub/keep.py", "a = 1\n")
        self.commit("tree")
        self.write("other/q.py", "b = 2\n")
        self.assertEqual(self.ranges("--base", "HEAD", cwd=self.root / "src/x"), "other/q.py:1-1")

    def test_untracked_lines_are_numbered_by_newlines_only(self):
        self.git_repo()
        (self.root / "ff.py").write_text("a = 1\n\x0cb = 2\nc = 3\n")
        self.assertEqual(self.ranges("--base", "HEAD"), "ff.py:1-3")

    def test_quoted_and_spaced_paths_parse(self):
        quoted = ('diff --git "a/caf\\303\\251 x.py" "b/caf\\303\\251 x.py"\n--- "a/caf\\303\\251 x.py"\n'
                  '+++ "b/caf\\303\\251 x.py"\n@@ -0,0 +1 @@\n+a = 1\n')
        self.assertEqual(parse_unified_diff(quoted)["café x.py"].added, {1: "a = 1"})
        spaced = "diff --git a/my file.py b/my file.py\n--- a/my file.py\n+++ b/my file.py\n@@ -0,0 +1 @@\n+b\n"
        self.assertEqual(list(parse_unified_diff(spaced)), ["my file.py"])
        tricky = ("diff --git a/my b/x.py b/my b/x.py\nnew file mode 100644\n--- /dev/null\n+++ b/my b/x.py\n"
                  "@@ -0,0 +1 @@\n+c\n")
        self.assertEqual(list(parse_unified_diff(tricky)), ["my b/x.py"])


class StageRunner(Scratch):
    CHECK = """\
        #!/usr/bin/env bash
        cd "$(dirname "$0")/.."
        source scripts/slopbrake/run-stages.sh
        STAGES=(a b c)
        SLOW_STAGES=(c)
        stage_a() { echo a; }
        stage_b() { echo "b: skipped: nothing to check"; return 78; }
        stage_c() { [ -z "${FAIL_C:-}" ]; }
        run_stages "$@"
        """

    def setUp(self):
        super().setUp()
        self.git_repo()
        self.write("scripts/slopbrake/run-stages.sh", (GUARDS / "run-stages.sh").read_text())
        self.executable("scripts/check", self.CHECK)
        self.commit("gate")
        self.head = self.git("rev-parse", "HEAD")

    def check(self, *args, **env):
        return sh(["scripts/check", *args], self.root, **env)

    def test_skip_code_is_reported_as_skip_and_does_not_fail(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r"skip  b \(\d+s\)")
        self.assertIn("pass  a", result.stdout)

    def test_a_full_clean_green_run_records_last_green(self):
        self.assertEqual(self.last_green(), "")
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(self.last_green(), self.head)

    def test_last_green_is_per_branch_and_never_detached(self):
        self.assertEqual(self.check().returncode, 0)
        self.git("checkout", "-q", "-b", "feat")
        self.write("NOTES.md", "x\n")
        self.commit("doc")
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(self.last_green("feat"), self.git("rev-parse", "HEAD"))
        self.assertEqual(self.last_green("main"), self.head)
        self.git("checkout", "-q", "--detach", "HEAD~1")
        self.check()
        refs = self.git("for-each-ref", "--format=%(refname)", "refs/slopbrake/").split()
        self.assertEqual(refs, [GREEN + "feat", GREEN + "main"])

    def test_partial_red_dirty_or_opted_out_runs_do_not_record(self):
        runs = {"fast": (["--fast"], {}), "stage list": (["a", "b", "c"], {}),
                "red": ([], {"FAIL_C": "1"}), "opt-out": ([], {"SLOPBRAKE_NO_RECORD": "1"})}
        for name, (args, env) in runs.items():
            with self.subTest(name):
                self.check(*args, **env)
                self.assertEqual(self.last_green(), "")
        self.write("scratch.py", "x = 1\n")
        self.check()
        self.assertEqual(self.last_green(), "")

    def test_an_explicit_base_that_skips_unverified_commits_does_not_record(self):
        self.git("update-ref", GREEN + "main", "HEAD")
        self.write("shop.py", "x = 1\n")
        self.commit("unverified")
        self.check(SLOPBRAKE_BASE="HEAD")
        self.assertEqual(self.last_green(), self.head)
        self.check(SLOPBRAKE_BASE=self.head)
        self.assertEqual(self.last_green(), self.git("rev-parse", "HEAD"))


class PythonTemplate(Scratch):
    SOURCE = "def discount(total, member):\n    if member and total >= 100:\n        return total - 10\n    return total\n"
    WEAK = ("import unittest\nfrom shop import discount\n\n\nclass T(unittest.TestCase):\n"
            "    def test_it(self):\n        discount(150, True)\n")

    def setUp(self):
        super().setUp()
        self.git_repo()
        template = (KIT / "python/scripts/check").read_text()
        for key, value in (("LINT_CMD", "true"), ("TYPES_CMD", "true"),
                           ("TEST_CMD", "python3 -m unittest discover -s tests")):
            template = template.replace(f"@{key}@", value)
        shutil.copytree(GUARDS, self.root / "scripts/slopbrake")
        self.executable("scripts/check", template)
        self.commit("gate")

    def test_env_cannot_lower_the_floor_or_swap_the_test_command(self):
        self.write("shop.py", self.SOURCE)
        self.write("tests/test_shop.py", self.WEAK)
        result = sh(["scripts/check", "mutation"], self.root, MUTATION_FLOOR="0")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("floor 60%", result.stdout)
        self.write("tests/test_shop.py", self.WEAK + "        self.fail('red')\n")
        self.assertEqual(sh(["scripts/check", "tests"], self.root, TEST_CMD="true").returncode, 1)

    def test_missing_smoke_entry_point_is_a_skip(self):
        result = sh(["scripts/check", "smoke"], self.root)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("smoke: skipped:", result.stdout)
        self.assertIn("skip  smoke", result.stdout)


class Mutation(Scratch):
    HEADER = "import unittest\nfrom shop import {names}\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n"

    def seed(self, source, body, names):
        self.write("shop.py", source)
        self.write("tests/test_shop.py", self.HEADER.format(names=names) + textwrap.indent(textwrap.dedent(body), " " * 8))

    def floor(self, *args, cwd=None, **env):
        return script("mutation_py.py", *args, cwd=cwd or self.root, **env)

    def test_commits_on_the_default_branch_are_checked_since_last_green(self):
        self.git_repo()
        self.git("update-ref", GREEN + "main", "HEAD")
        self.seed("def tax(x):\n    return x * 2 + 1\n", "tax(500)\n", "tax")
        self.commit("committed on main")
        result = self.floor()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("below floor", result.stdout)
        self.assertIn("last-green", result.stdout)

    def test_no_base_is_a_loud_skip(self):
        self.git_repo(branch="trunk")
        self.seed("def tax(x):\n    return x * 2\n", "tax(500)\n", "tax")
        result = self.floor()
        self.assertEqual(result.returncode, SKIP, result.stdout)
        self.assertIn("mutation: skipped: no base ref", result.stdout)

    def test_orphan_branch_is_checked_as_all_new(self):
        self.git_repo()
        self.git("checkout", "-q", "--orphan", "fresh")
        self.seed("def tax(x):\n    return x * 2\n", "tax(500)\n", "tax")
        self.commit("orphan")
        result = self.floor("--base", "main")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("below floor", result.stdout)

    def test_nothing_changed_and_nothing_mutable_are_skips(self):
        self.git_repo()
        result = self.floor("--base", "main")
        self.assertEqual(result.returncode, SKIP)
        self.assertIn("mutation: skipped:", result.stdout)
        self.seed("def notify(user):\n    pass\n", "notify('a')\n", "notify")
        result = self.floor("--base", "main")
        self.assertEqual(result.returncode, SKIP, result.stdout)
        self.assertIn("no mutable sites", result.stdout)

    def test_boundary_mutants_catch_untested_edges(self):
        self.git_repo()
        self.seed("def can_vote(age):\n    return age >= 18\n",
                  "self.assertTrue(can_vote(40))\nself.assertFalse(can_vote(3))\n", "can_vote")
        result = self.floor("--base", "main")
        self.assertIn("[>= -> >]", result.stdout)

    def test_string_constants_and_dropped_calls_are_mutated(self):
        self.git_repo()
        self.seed('def label(k):\n    return {"a": "alpha"}.get(k, "none")\n\n\n'
                  'def notify(user, mailer):\n    mailer.send(f"hi {user}")\n    mailer.close()\n',
                  'self.assertTrue(label("a"))\nnotify("ada", __import__("unittest.mock").mock.Mock())\n',
                  "label, notify")
        result = self.floor("--base", "main")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("shop.py:2 [string 'alpha'", result.stdout)
        self.assertIn("shop.py:6 [delete call]", result.stdout)
        self.assertIn("shop.py:7 [delete call]", result.stdout)
        self.assertNotIn("'hi '", result.stdout)  # f-string parts are not string mutants

    def test_lookup_strings_are_not_crash_killed(self):
        self.git_repo()
        self.seed('import json\n\n\ndef load(path, cfg):\n    with open(path, encoding="utf-8") as f:\n'
                  '        data = json.load(f)\n    host = cfg["host"]\n    return f"{host}:{data[\'port\']}"\n',
                  'import json, os, tempfile\nfd, p = tempfile.mkstemp()\nos.write(fd, json.dumps({"port": 1}).encode())\n'
                  'os.close(fd)\nload(p, {"host": "h"})\n', "load")
        result = self.floor("--base", "main")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("0/1 mutants killed", result.stdout)

    def test_messages_and_names_are_not_string_mutants(self):
        self.git_repo()
        self.seed('"""Shop."""\nimport logging\n__all__ = ["tax"]\n"Bare note."\nlog = logging.getLogger("shop")\n\n\n'
                  'def tax(x):\n    if x < 0:\n        raise ValueError("negative")\n    return x\n\n\n'
                  'if __name__ == "__main__":\n    pass\n',
                  "self.assertEqual(tax(5), 5)\n", "tax")
        result = self.floor("--base", "main")
        self.assertNotIn("[string", result.stdout)

    def test_string_concatenation_is_not_killed_by_a_crash(self):
        self.git_repo()
        self.seed('def greet(name, title):\n    return "Dear " + title + " " + name + ","\n\n\n'
                  'def summary(items):\n    return ", ".join(items) + " (" + str(len(items)) + " items)"\n',
                  'greet("Ada", "Dr")\nsummary(["a", "b"])\n', "greet, summary")
        result = self.floor("--base", "main")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("[+ -> -]", result.stdout)

    def test_no_mutate_pragma_skips_the_line(self):
        self.git_repo()
        self.seed("CACHE = 128  # slopbrake: no-mutate\n", "pass\n", "CACHE")
        result = self.floor("--base", "main")
        self.assertEqual(result.returncode, SKIP, result.stdout)
        self.assertIn("no mutable sites", result.stdout)
        self.assertIn("1 changed line excluded by no-mutate", result.stdout)

    def test_no_mutate_lines_are_reported_in_the_score(self):
        self.git_repo()
        self.seed("def tax(x):\n    return x * 2  # slopbrake: no-mutate\n\n\ndef fee(x):\n    return x + 1\n",
                  "tax(1)\nfee(1)\n", "tax, fee")
        result = self.floor("--base", "main")
        self.assertIn("mutation: 1 changed line excluded by no-mutate", result.stdout)
        self.assertIn("below floor", result.stdout)

    def test_form_feeds_do_not_shift_the_pragma(self):
        self.git_repo()
        self.seed("", "h(5)\n", "h")  # seed() dedents, which would blank the form-feed line
        (self.root / "shop.py").write_text("def h(x):\n    \x0c\n    if x > 1:\n"
                                           "        return 2  # slopbrake: no-mutate\n    return 3\n")
        result = self.floor("--base", "main")
        self.assertIn("survived: shop.py:5 [return None] return 3", result.stdout)
        self.assertNotIn("shop.py:4", result.stdout)

    def test_non_ascii_paths_are_checked(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feat")
        self.write("calc/__init__.py", "")
        self.write("calc/tarifé.py", "def rate(x):\n    return x * 2\n")
        self.write("tests/test_rate.py", "import unittest\nfrom calc.tarifé import rate\n\n\n"
                                         "class T(unittest.TestCase):\n    def test_it(self):\n        rate(2)\n")
        self.commit("accented")
        result = self.floor("--base", "main")
        self.assertIn("survived: calc/tarifé.py:2", result.stdout)

    def test_a_timed_out_mutant_takes_its_whole_process_group_down(self):
        self.git_repo()
        pids = self.aux / "pids"
        self.write("calc/__init__.py", "")
        self.write("calc/loop.py", "def count_up(n):\n    i = 0\n    while i < n:\n        i += 1\n    return i\n")
        self.write("tests/test_loop.py", f"""\
            import os
            import unittest
            from calc.loop import count_up
            with open({str(pids)!r}, "a") as f:
                f.write(f"{{os.getpid()}}\\n")
            class T(unittest.TestCase):
                def test_it(self):
                    self.assertEqual(count_up(3), 3)
            """)
        self.addCleanup(self.kill_all, pids)
        self.floor("--base", "main", "--test-cmd", "python3 -m unittest discover -s tests && true")
        time.sleep(1)
        self.assertEqual([pid for pid in self.read_pids(pids) if self.alive(pid)], [])

    def test_sigterm_removes_the_scratch_copy(self):
        self.git_repo()
        tmpdir = self.aux / "tmp"
        tmpdir.mkdir()
        self.seed("def tax(x):\n    return x * 2\n", "import time; time.sleep(60)\n", "tax")
        proc = subprocess.Popen([sys.executable, str(GUARDS / "mutation_py.py"), "--base", "main"], cwd=self.root,
                                env=dict(CLEAN_ENV, TMPDIR=str(tmpdir)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while not list(tmpdir.glob("slopbrake-mutation-*/repo/tests")) and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
        self.assertEqual(list(tmpdir.iterdir()), [])

    @staticmethod
    def read_pids(path):
        return [int(p) for p in path.read_text().split()] if path.exists() else []

    @staticmethod
    def alive(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
        except (OSError, IndexError):
            return False

    def kill_all(self, path):
        for pid in self.read_pids(path):
            if self.alive(pid):
                os.kill(pid, signal.SIGKILL)


class PrePush(Scratch):
    def setUp(self):
        super().setUp()
        self.git_repo()
        self.log = self.aux / "check.log"
        self.executable("scripts/check", f"""\
            #!/usr/bin/env bash
            v="base=${{SLOPBRAKE_BASE-unset}} floor=${{MUTATION_FLOOR-unset}} test=${{TEST_CMD-unset}}"
            echo "$v gh=${{GITHUB_BASE_REF-unset}} args=$*" >> {str(self.log)!r}
            [ -z "${{FAIL:-}}" ]
            """)
        self.commit("gate")
        self.head = self.git("rev-parse", "HEAD")
        self.first = self.git("rev-parse", "HEAD~1")

    def push(self, *lines, **env):
        result = sh([str(GITHOOKS / "pre-push"), "origin", "url"], self.root, stdin="".join(l + "\n" for l in lines), **env)
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return result, logged

    def test_checks_each_pushed_ref_against_the_remote_tip(self):
        zero = "0" * 40
        _, logged = self.push(f"refs/heads/main {self.head} refs/heads/main {self.first}",
                              f"refs/heads/new {self.head} refs/heads/new {zero}",
                              f"(delete) {zero} refs/heads/old {self.first}",
                              f"refs/heads/x {self.head} refs/heads/x {'1' * 40}")
        self.assertEqual(logged, [f"base={self.first} floor=unset test=unset gh=unset args=",
                                  "base=unset floor=unset test=unset gh=unset args="])

    def test_inherited_overrides_are_dropped(self):
        _, logged = self.push(f"refs/heads/main {self.head} refs/heads/main {self.first}",
                              SLOPBRAKE_BASE="HEAD", MUTATION_FLOOR="0", TEST_CMD="true", GITHUB_BASE_REF="main")
        self.assertEqual(logged, [f"base={self.first} floor=unset test=unset gh=unset args="])

    def test_a_red_gate_blocks_the_push(self):
        result, _ = self.push(f"refs/heads/main {self.head} refs/heads/main {self.first}", FAIL="1")
        self.assertNotEqual(result.returncode, 0)

    def test_a_ref_that_is_not_checked_out_cannot_be_pushed_unchecked(self):
        self.git("branch", "old", "HEAD~1")
        result, logged = self.push(f"refs/heads/old {self.first} refs/heads/old {'0' * 40}")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("check out refs/heads/old to push it", result.stderr)
        self.assertEqual(logged, [])

    def test_a_ref_already_on_the_remote_needs_no_check(self):
        self.git("update-ref", "refs/remotes/origin/main", self.first)
        result, logged = self.push(f"refs/tags/v1 {self.first} refs/tags/v1 {'0' * 40}")
        self.assertEqual((result.returncode, logged), (0, []), result.stderr)

    def test_an_annotated_tag_at_head_is_checked_like_head(self):
        self.git("tag", "-a", "-m", "release", "v2")
        result, logged = self.push(f"refs/tags/v2 {self.git('rev-parse', 'v2')} refs/tags/v2 {'0' * 40}")
        self.assertEqual((result.returncode, len(logged)), (0, 1), result.stderr)


class PreCommit(Scratch):
    def test_warns_when_staged_files_also_have_unstaged_changes(self):
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
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unstaged changes", result.stderr)
        self.assertIn("calc.py", result.stderr)


class StopHook(Scratch):
    def setUp(self):
        super().setUp()
        self.git_repo()
        self.log = self.aux / "check.log"
        self.executable("scripts/check", f"""\
            #!/usr/bin/env bash
            cd "$(dirname "$0")/.."
            echo "$PWD args=$* max=${{MUTATION_MAX-unset}}" >> {str(self.log)!r}
            [ -e SLOW ] && sleep 10
            if [ -e RED ] || [ -n "${{FAIL:-}}" ]; then echo "FAIL  tests"; exit 1; fi
            """)
        self.commit("gate")

    def stop(self, payload, env=None, **extra):
        env = dict(env or CLEAN_ENV, **{"CLAUDE_PROJECT_DIR": str(self.root), **extra})
        result = sh([str(HOOKS / "require-green.sh")], self.root, env=env, stdin=json.dumps(payload))
        logged = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return result, logged

    def worktree(self):
        wt = self.aux / "wt"
        self.git("worktree", "add", "-q", "-b", "feat", str(wt))
        (wt / "sub").mkdir()
        (wt / "RED").write_text("red\n")
        return wt

    def test_red_worktree_blocks_the_stop(self):
        wt = self.worktree()
        result, logged = self.stop({"session_id": "s1", "cwd": str(wt / "sub")})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(str(wt), result.stderr)
        self.assertEqual(logged, [f"{wt} args=--fast max=unset"])

    def test_parses_the_payload_without_jq(self):
        wt = self.worktree()
        nojq = self.aux / "bin"
        nojq.mkdir()
        for directory in os.environ["PATH"].split(os.pathsep):
            for tool in Path(directory).glob("*") if Path(directory).is_dir() else ():
                if tool.name != "jq" and not (nojq / tool.name).exists():
                    (nojq / tool.name).symlink_to(tool)
        result, _ = self.stop({"session_id": "s1", "cwd": str(wt)}, env=dict(CLEAN_ENV, PATH=str(nojq)))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue((self.root / ".git/slopbrake/stop-red-s1").exists())

    def test_committed_work_since_last_green_gets_the_full_gate(self):
        self.git("update-ref", GREEN + "main", "HEAD~1")
        result, logged = self.stop({"session_id": "s2"}, FAIL="1")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(logged, [f"{self.root} args= max=40"])
        self.assertIn("FAIL  tests", result.stderr)
        self.git("update-ref", GREEN + "main", "HEAD")
        result, logged = self.stop({"session_id": "s2"}, FAIL="1")
        self.assertEqual((result.returncode, logged), (0, []))

    def test_the_full_gate_is_bounded(self):
        self.git("update-ref", GREEN + "main", "HEAD~1")
        _, logged = self.stop({"session_id": "s5"}, SLOPBRAKE_STOP_MUTANTS="7")
        self.assertEqual(logged, [f"{self.root} args= max=7"])
        (self.root / ".git/info/exclude").write_text("SLOW\n")
        (self.root / "SLOW").write_text("")
        started = time.monotonic()
        result, _ = self.stop({"session_id": "s5"}, SLOPBRAKE_STOP_TIMEOUT="1")
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("timed out", result.stderr)

    def test_one_time_budget_covers_the_project_and_the_worktree(self):
        wt = self.worktree()
        (self.root / ".git/info/exclude").write_text("SLOW\n")
        for repo in (self.root, wt):
            (repo / "SLOW").write_text("")
        (self.root / "RED").write_text("red\n")  # both dirty: both get --fast
        started = time.monotonic()
        result, _ = self.stop({"session_id": "s7", "cwd": str(wt)}, SLOPBRAKE_STOP_TIMEOUT="4")
        self.assertLess(time.monotonic() - started, 6.5)  # one 4 s budget, not 4 s per repo
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr.count("timed out"), 2, result.stderr)
        self.assertIn("scripts/check --fast timed out", result.stderr)

    def test_worktree_of_a_monorepo_subdirectory_install(self):
        app = self.root / "app"
        app.mkdir()
        shutil.move(str(self.root / "scripts"), str(app / "scripts"))
        self.commit("move the install into app/")
        wt = self.aux / "wt"
        self.git("worktree", "add", "-q", "-b", "feat", str(wt))
        (wt / "app/RED").write_text("red\n")
        result, logged = self.stop({"session_id": "s6", "cwd": str(wt)}, CLAUDE_PROJECT_DIR=str(app))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(logged, [f"{wt}/app args=--fast max=unset"])

    def test_three_red_stops_then_it_lets_go(self):
        (self.root / "RED").write_text("red\n")
        codes = [self.stop({"session_id": "s3"})[0].returncode for _ in range(4)]
        self.assertEqual(codes, [2, 2, 0, 2])

    def test_docs_only_changes_skip_the_gate(self):
        self.write("NOTES.md", "x\n")
        result, logged = self.stop({"session_id": "s4"})
        self.assertEqual((result.returncode, logged), (0, []))


if __name__ == "__main__":
    unittest.main()
