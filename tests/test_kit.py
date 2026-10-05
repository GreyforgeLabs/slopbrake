"""Behaviour tests for Slopbrake's checks and its installer, through their command lines."""
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
HOOKS = HOME / "slopbrake/kit/common/.claude/hooks"
RULES = HOME / "slopbrake/kit/common/.claude/door-rules.yml"
sys.path.insert(0, str(GUARDS))

from common import glob_to_regex, load_simple_yaml, parse_unified_diff

GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]


def sh(cmd, cwd, env=None, stdin=None):
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False)


def script(name, *args, cwd, env=None):
    return sh([sys.executable, str(GUARDS / name), *args], cwd, env)


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

    def git_repo(self):
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("README.md", "# demo\n")
        self.commit("init")

    def commit(self, message):
        sh(["git", "add", "-A"], self.root)
        result = sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", message], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)


class Globs(unittest.TestCase):
    def test_double_star_prefix_matches_at_any_depth_including_root(self):
        rx = glob_to_regex("**/migrations/**")
        self.assertTrue(rx.match("migrations/0001.sql"))
        self.assertTrue(rx.match("db/migrations/2026/0001.sql"))
        self.assertFalse(rx.match("docs/migrations.md"))

    def test_single_star_stays_within_a_directory(self):
        self.assertTrue(glob_to_regex("infra/*.tf").match("infra/main.tf"))
        self.assertFalse(glob_to_regex("infra/*.tf").match("infra/mod/main.tf"))


class RuleFiles(unittest.TestCase):
    def test_yaml_subset_keeps_regex_escapes_and_drops_comments(self):
        data = load_simple_yaml('one_way:\n  - "a/**"  # note\ncontent_patterns:\n  - "sendEmail\\\\("\ndefault: two_way\n')
        self.assertEqual(data, {"one_way": ["a/**"], "content_patterns": ["sendEmail\\("], "default": "two_way"})

    def test_kit_door_rules_parse(self):
        data = load_simple_yaml(RULES.read_text())
        self.assertIn("**/migrations/**", data["one_way"])
        self.assertEqual(data["default"], "two_way")

    def test_diff_parser_reads_added_line_numbers(self):
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -3,0 +4,2 @@\n+a = 1\n+b = 2\n"
        self.assertEqual(parse_unified_diff(diff)["x.py"].added, {4: "a = 1", 5: "b = 2"})

    def test_diff_parser_keeps_added_lines_that_start_with_plus_plus(self):
        diff = "diff --git a/x.sql b/x.sql\n--- a/x.sql\n+++ b/x.sql\n@@ -0,0 +1,2 @@\n+++ DROP TABLE users;\n+b\n"
        self.assertEqual(parse_unified_diff(diff)["x.sql"].added, {1: "++ DROP TABLE users;", 2: "b"})


class DoorClassify(Scratch):
    def classify(self, diff):
        self.write("change.diff", diff)
        result = script("door_classify.py", "--rules", str(RULES), "--diff-file", "change.diff", "--json", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_new_migration_is_one_way(self):
        diff = ("diff --git a/migrations/0002_x.sql b/migrations/0002_x.sql\nnew file mode 100644\n"
                "--- /dev/null\n+++ b/migrations/0002_x.sql\n@@ -0,0 +1 @@\n+ALTER TABLE t ADD c int;\n")
        self.assertEqual(self.classify(diff)["door"], "one-way")

    def test_readme_typo_is_two_way(self):
        diff = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# Teh\n+# The\n"
        self.assertEqual(self.classify(diff), {"door": "two-way", "reasons": [], "files": 1, "base": None})

    def test_destructive_sql_in_source_is_one_way_but_not_in_tests(self):
        source = "diff --git a/src/purge.ts b/src/purge.ts\n--- a/src/purge.ts\n+++ b/src/purge.ts\n@@ -1,0 +2 @@\n+db.run(\"DELETE FROM users\");\n"
        test = source.replace("src/purge.ts", "tests/purge.test.ts")
        self.assertEqual(self.classify(source)["door"], "one-way")
        self.assertEqual(self.classify(test)["door"], "two-way")

    def test_a_scalar_rule_is_an_error_not_a_list_of_characters(self):
        self.write("rules.yml", 'one_way: "migrations/**"\n')
        self.write("change.diff", "diff --git a/migrations/1.sql b/migrations/1.sql\n--- a/migrations/1.sql\n"
                                  "+++ b/migrations/1.sql\n@@ -0,0 +1 @@\n+x\n")
        result = script("door_classify.py", "--rules", "rules.yml", "--diff-file", "change.diff", cwd=self.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("one_way must be a list", result.stderr)

    def test_an_empty_inline_list_means_no_rules(self):
        self.assertEqual(load_simple_yaml("content_patterns: []\n"), {"content_patterns": []})

    def test_empty_scalars_are_rejected_for_every_rule_list(self):
        self.write("change.diff", "diff --git a/README.md b/README.md\n--- a/README.md\n"
                                  "+++ b/README.md\n@@ -0,0 +1 @@\n+Documentation\n")
        for key in ("one_way", "content_patterns", "ignore", "content_ignore"):
            for scalar in ('""', "''"):
                with self.subTest(key=key, scalar=scalar):
                    self.write("rules.yml", f"{key}: {scalar}\n")
                    result = script("door_classify.py", "--rules", "rules.yml",
                                    "--diff-file", "change.diff", cwd=self.root)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"{key} must be a list", result.stderr)

    def test_missing_and_explicit_empty_rule_lists_remain_valid(self):
        self.write("change.diff", "diff --git a/README.md b/README.md\n--- a/README.md\n"
                                  "+++ b/README.md\n@@ -0,0 +1 @@\n+Documentation\n")
        for content in ("", "one_way: []\ncontent_patterns: []\nignore: []\ncontent_ignore: []\n"):
            with self.subTest(content=content):
                self.write("rules.yml", content)
                result = script("door_classify.py", "--rules", "rules.yml",
                                "--diff-file", "change.diff", "--json", cwd=self.root)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["door"], "two-way")

    def test_changing_the_classifier_itself_is_one_way(self):
        diff = "diff --git a/.claude/door-rules.yml b/.claude/door-rules.yml\n--- a/.claude/door-rules.yml\n+++ b/.claude/door-rules.yml\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertEqual(self.classify(diff)["door"], "one-way")


BODY = """## Summary

```text
f -> g
```

## Evidence

- **Before:** test failed
  **After:** test passes

## Merge Danger

**Door:** {door}

{extra}

**Blast Radius:** {radius}

## Review log

- none

## Open questions

- none
"""


class PrBody(Scratch):
    def check(self, body):
        self.write("body.md", body)
        return script("pr_body_check.py", "--body-file", "body.md", "--no-door-floor", cwd=self.root)

    def test_complete_two_way_body_passes(self):
        self.assertEqual(self.check(BODY.format(door="two-way", extra="", radius="ui")).returncode, 0)

    def test_missing_door_and_empty_blast_radius_fail(self):
        result = self.check(BODY.format(door="two-way", extra="", radius="").replace("**Door:** two-way", ""))
        self.assertEqual(result.returncode, 1)
        self.assertIn("exactly one '**Door:**", result.stdout)
        self.assertIn("Blast Radius", result.stdout)

    def test_one_way_needs_a_rollback_plan(self):
        self.assertEqual(self.check(BODY.format(door="one-way", extra="", radius="db")).returncode, 1)
        ok = BODY.format(door="one-way", extra="Rollback: restore from backup.", radius="db")
        self.assertEqual(self.check(ok).returncode, 0)

    def test_door_inside_a_code_block_does_not_count(self):
        body = BODY.format(door="two-way", extra="", radius="ui").replace("**Door:** two-way", "```\n**Door:** two-way\n```")
        self.assertEqual(self.check(body).returncode, 1)

    def test_under_declared_door_fails_against_the_computed_floor(self):
        self.git_repo()
        self.write(".claude/door-rules.yml", RULES.read_text())
        self.commit("rules")
        base = sh(["git", "rev-parse", "HEAD"], self.root).stdout.strip()
        self.write("migrations/0002_drop.sql", "DROP TABLE users;\n")
        self.commit("migration")
        self.write("body.md", BODY.format(door="two-way", extra="", radius="db"))
        result = script("pr_body_check.py", "--body-file", "body.md", "--base", base, cwd=self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("never lowered", result.stdout)

    def test_uncommitted_changes_are_flagged_because_the_floor_ignores_them(self):
        self.git_repo()
        self.write(".claude/door-rules.yml", RULES.read_text())
        self.commit("rules")
        self.write("migrations/0002_drop.sql", "DROP TABLE users;\n")
        self.write("body.md", BODY.format(door="two-way", extra="", radius="db"))
        result = script("pr_body_check.py", "--body-file", "body.md", "--base", "HEAD", cwd=self.root)
        self.assertIn("uncommitted", result.stdout)

    def test_outside_pr_context_is_skipped(self):
        env = {k: v for k, v in os.environ.items() if k not in ("PR_BODY_FILE", "GITHUB_EVENT_PATH")}
        result = script("pr_body_check.py", "--no-door-floor", cwd=self.root, env=env)
        self.assertEqual(result.returncode, 78)
        self.assertTrue(result.stdout.startswith("pr: skipped: no PR context"), result.stdout)


class Tautology(Scratch):
    def flagged_lines(self, source):
        self.write("tests/test_seed.py", source)
        result = script("tautology_py.py", "tests", cwd=self.root)
        return result.returncode, sorted(int(line.split(":")[1]) for line in result.stdout.splitlines() if ": tautological" in line)

    def test_pocock_examples_are_flagged(self):
        code, lines = self.flagged_lines("""\
            from shop import MAX_LEN, add, total, truncate
            def test_a():
                assert MAX_LEN == 280
            def test_b():
                items = [{"price": 10}, {"price": 5}]
                expected = sum(i["price"] for i in items)
                assert total(items) == expected
            def test_c():
                a, b = 2, 3
                assert add(a, b) == a + b
            def test_d():
                assert truncate("x" * 300) == "x" * MAX_LEN
            def test_e():
                assert True
            """)
        self.assertEqual((code, lines), (1, [3, 7, 10, 12, 14]))

    def test_independent_expectations_and_property_checks_pass(self):
        code, lines = self.flagged_lines("""\
            from shop import ERR_TOO_LONG, sort_all, total, validate
            def test_a():
                assert total([{"price": 10}, {"price": 5}]) == 15
            def test_b():
                assert validate("x" * 300) == ERR_TOO_LONG
            def test_c():
                out = sort_all([3, 1, 2])
                assert out == sorted(out)
            def test_d(tmp_path):
                before = (tmp_path / "f").read_bytes()
                assert (tmp_path / "f").read_bytes() == before
            """)
        self.assertEqual((code, lines), (0, []))

    def test_allow_comment_needs_a_reason(self):
        _, lines = self.flagged_lines("""\
            from shop import MAX_LEN
            def test_a():
                # slopbrake: allow-tautology: 280 is the published API limit
                assert MAX_LEN == 280
            def test_b():
                # slopbrake: allow-tautology:
                assert MAX_LEN == 280
            """)
        self.assertEqual(lines, [7])


class Boundaries(Scratch):
    def test_deep_imports_private_names_and_cycles_are_violations(self):
        self.write("shop/__init__.py", "from .lib.impl import total\n")
        self.write("shop/lib/__init__.py", "")
        self.write("shop/lib/impl.py", "def total():\n    return 1\n")
        self.write("shop/pricing.py", "def _secret():\n    pass\n")
        self.write("tests/test_ok.py", "from shop import total\nfrom shop.pricing import price\n")
        self.write("tests/test_bad.py", "from shop.lib.impl import total\nfrom shop.pricing import _secret\n")
        self.write("a.py", "import b\n")
        self.write("b.py", "import a\n")
        self.write("c.py", "def f():\n    import a\n")
        result = script("boundaries_py.py", ".", cwd=self.root)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines()[:-1], [
            "tests/test_bad.py:1: tests-through-entrypoints: imports subpackage shop.lib; use shop's entry points",
            "tests/test_bad.py:2: tests-through-entrypoints: imports private name shop.pricing._secret; use shop's entry points",
            "no-circular: a -> b -> a",
        ])

    def test_baseline_accepts_legacy_violations_but_not_new_ones(self):
        self.write("shop/__init__.py", "")
        self.write("shop/lib/__init__.py", "")
        self.write("tests/test_old.py", "from shop.lib import x\n")
        self.assertEqual(script("boundaries_py.py", ".", "--baseline", "bl.txt", "--write-baseline", cwd=self.root).returncode, 0)
        self.assertEqual(script("boundaries_py.py", ".", "--baseline", "bl.txt", cwd=self.root).returncode, 0)
        self.write("tests/test_new.py", "from shop.lib import y\n")
        self.assertEqual(script("boundaries_py.py", ".", "--baseline", "bl.txt", cwd=self.root).returncode, 1)


class Mutation(Scratch):
    SOURCE = "def discount(total, member):\n    if member and total >= 100:\n        return total - 10\n    return total\n"
    HEADER = "import unittest\nfrom shop import discount\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n"

    def run_floor(self, assertions):
        self.git_repo()
        self.write("shop.py", self.SOURCE)
        self.write("tests/test_shop.py", self.HEADER + assertions)
        return script("mutation_py.py", "--base", "main", cwd=self.root)

    def test_assertion_free_test_is_below_the_floor(self):
        result = self.run_floor("        self.assertIsNotNone(discount(150, True))\n")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("survived: shop.py:2", result.stdout)

    def test_real_assertions_clear_the_floor(self):
        result = self.run_floor("".join(f"        self.assertEqual(discount({a}), {b})\n" for a, b in
                                        (("150, True", 140), ("100, True", 90), ("99, True", 99), ("150, False", 150))))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("7/7 mutants killed", result.stdout)

    def test_working_tree_is_left_untouched(self):
        self.run_floor("        self.assertIsNotNone(discount(150, True))\n")
        self.assertEqual((self.root / "shop.py").read_text(), self.SOURCE)

    def test_user_diff_settings_do_not_hide_changes(self):
        self.git_repo()
        for key, value in (("diff.mnemonicPrefix", "true"), ("diff.noprefix", "true"), ("color.diff", "always")):
            sh(["git", "config", key, value], self.root)
        self.write("shop.py", "def base():\n    return 1\n")
        self.commit("tracked module")  # a modified tracked file goes through the diff parser
        self.write("shop.py", "def base():\n    return 1\n\n\n" + self.SOURCE)
        self.write("tests/test_shop.py", self.HEADER + "        self.assertIsNotNone(discount(150, True))\n")
        result = script("mutation_py.py", "--base", "main", cwd=self.root)
        self.assertIn("survived: shop.py:6", result.stdout)

    def test_nothing_changed_is_skipped(self):
        self.git_repo()
        result = script("mutation_py.py", "--base", "main", cwd=self.root)
        self.assertEqual(result.returncode, 0)
        self.assertIn("skipped", result.stdout)


class GateInsideGitHooks(Scratch):
    def test_tests_that_use_git_do_not_corrupt_the_commit_run_by_the_hook(self):
        self.git_repo()
        runner = (GUARDS / "run-stages.sh").read_text()
        self.write("scripts/slopbrake/run-stages.sh", runner)
        self.write("scripts/check", """\
            #!/usr/bin/env bash
            cd "$(dirname "$0")/.."
            source scripts/slopbrake/run-stages.sh
            STAGES=(tests)
            stage_tests() {
              other=$(mktemp -d) && cd "$other" && git init -q && echo x > stray.txt && git add stray.txt
            }
            run_stages "$@"
            """)
        self.write(".githooks/pre-commit", '#!/usr/bin/env bash\nexec scripts/check --fast\n')
        for rel in ("scripts/check", ".githooks/pre-commit"):
            (self.root / rel).chmod(0o755)
        sh(["git", "config", "core.hooksPath", ".githooks"], self.root)
        self.commit("add gate")
        self.write("README.md", "# changed\n")
        result = sh(["git", *GIT_ID, "commit", "-q", "-am", "edit"], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("stray.txt", sh(["git", "ls-files"], self.root).stdout)


class GitGuard(unittest.TestCase):
    def exit_code(self, command):
        payload = json.dumps({"tool_input": {"command": command}})
        return sh([str(HOOKS / "block-dangerous-git.sh")], None, stdin=payload).returncode

    def test_destructive_commands_are_blocked(self):
        for command in ("git push --force origin main", "git push -f", "git push origin +main",
                        "git reset --hard HEAD~1", "git clean -fd", "git branch -D old", "git checkout .",
                        "git checkout -- .", "git restore .", "cd x && git -C y reset --hard",
                        "git -c core.editor=vi push --force", "git --no-pager push -f", "git clean --force -d",
                        "git checkout HEAD -- .", "git checkout main -- .",
                        "git restore --source HEAD .", "git restore --source=HEAD .",
                        "git restore -s HEAD .", "git branch --delete --force old"):
            for separator in (" ", "  ", "\t", " \t "):
                spaced = command.replace(" ", separator)
                with self.subTest(command=spaced):
                    self.assertEqual(self.exit_code(spaced), 2)

    def test_everyday_commands_pass(self):
        for command in ("git push origin feature/x", "git push -u origin HEAD", "git status", "git checkout .github/x",
                        "git restore --staged .", "git restore src/a.ts", "git clean -n", "git branch -d merged",
                        "git -c color.ui=never push origin main", "git checkout HEAD -- src/a.ts",
                        "git restore --source HEAD src/a.ts", "git restore --source=HEAD src/a.ts"):
            for separator in (" ", "  ", "\t", " \t "):
                spaced = command.replace(" ", separator)
                with self.subTest(command=spaced):
                    self.assertEqual(self.exit_code(spaced), 0)


class Installer(Scratch):
    def gf(self, *args):
        return sh([sys.executable, "-m", "slopbrake", *args], self.root, env=dict(os.environ, PYTHONPATH=str(HOME)))

    def python_repo(self):
        self.git_repo()
        self.write("pyproject.toml", "[project]\nname = 'shop'\n")
        self.write("shop.py", "def one():\n    return 1\n")
        self.write("tests/test_shop.py", "import unittest\nfrom shop import one\n\n\nclass T(unittest.TestCase):\n"
                                         "    def test_one(self):\n        self.assertEqual(one(), 1)\n")
        self.commit("app")

    def test_init_creates_the_spec_files_and_is_idempotent(self):
        self.python_repo()
        first = json.loads(self.gf("init", str(self.root), "--json").stdout)
        created = {a["path"] for a in first["actions"] if a["action"] == "create"}
        for rel in ("CLAUDE.md", "CODING_STANDARDS.md", ".claude/agents/reviewer.md", ".claude/skills/tdd/SKILL.md",
                    ".claude/skills/implement-ticket/SKILL.md", ".claude/door-rules.yml", "scripts/check"):
            self.assertIn(rel, created)
        self.assertTrue(os.access(self.root / "scripts/check", os.X_OK))
        self.assertIn("Our additions", (self.root / ".claude/skills/pr/SKILL.md").read_text())
        self.assertLessEqual(len((self.root / "CLAUDE.md").read_text().splitlines()), 40)
        self.assertIn(".ruff_cache/", (self.root / ".gitignore").read_text().splitlines())
        second = json.loads(self.gf("init", str(self.root), "--json").stdout)
        self.assertEqual({a["action"] for a in second["actions"]}, {"unchanged"})

    def test_init_never_overwrites_repo_owned_files_and_merges_settings(self):
        self.python_repo()
        self.write("CLAUDE.md", "# mine\n")
        self.write(".claude/settings.json", json.dumps({"model": "x", "hooks": {"Stop": []}}))
        result = json.loads(self.gf("init", str(self.root), "--json").stdout)
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "# mine\n")
        settings = json.loads((self.root / ".claude/settings.json").read_text())
        self.assertEqual(settings["model"], "x")
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)
        self.assertIn("merge", {a["action"] for a in result["actions"] if a["path"] == ".claude/settings.json"})

    def test_verify_explains_when_the_kit_is_not_committed_yet(self):
        self.python_repo()
        self.gf("init", str(self.root))
        result = self.gf("verify", str(self.root))
        self.assertEqual(result.returncode, 1)
        self.assertIn("commit the files `slopbrake init` wrote", result.stdout)

    def test_status_reports_a_repo_without_guardrails(self):
        self.git_repo()
        result = self.gf("status", str(self.root), "--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no guardrail: neither git hooks nor CI run scripts/check", json.loads(result.stdout)[0]["gaps"])


if __name__ == "__main__":
    unittest.main()
