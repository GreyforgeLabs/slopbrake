"""Door floor (G2) and PR body (G1, G3, G4, C3) behaviour, through the scripts' command lines."""
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
RULES = HOME / "slopbrake/kit/common/.claude/door-rules.yml"
GIT_ID = ["-c", "user.name=doors-test", "-c", "user.email=doors@test"]
PR_ENV = ("PR_BODY_FILE", "GITHUB_EVENT_PATH", "GITHUB_BASE_REF", "SLOPBRAKE_BASE", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY")
ON_MAIN = ("one-way door on main: commit it on a feature branch for operator review "
           "(a human may override with git commit --no-verify)")


def sh(cmd, cwd, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in PR_ENV}
    return env | extra


def script(name, *args, cwd, env=None):
    return sh([sys.executable, str(GUARDS / name), *args], cwd, env if env is not None else clean_env())


def new_file_diff(path, *lines):
    body = "".join(f"+{line}\n" for line in lines)
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


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

{log}

## Open questions

- none
"""


def body(door="two-way", extra="", radius="db", log="- none"):
    return BODY.format(door=door, extra=extra, radius=radius, log=log)


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

    def git(self, *args):
        result = sh(["git", *GIT_ID, *args], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def git_repo(self, branch="main", rules=True):
        self.git("init", "-q", "-b", branch)
        self.write("README.md", "# demo\n")
        if rules:
            self.write(".claude/door-rules.yml", RULES.read_text())
        self.commit("init")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "-m", message)

    def classify_json(self, *args):
        result = script("door_classify.py", "--json", *args, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


class MeasuringNothing(Scratch):
    def test_head_as_base_warns_that_nothing_is_measured(self):
        self.git_repo()
        self.write("db/migrations/0002_drop.sql", "DROP TABLE users;\n")
        self.commit("drop users")
        result = script("door_classify.py", "--json", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("measuring nothing: HEAD is the base (main)", result.stderr)

    def test_a_real_base_has_no_warning(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feature")
        self.write("db/migrations/0002_drop.sql", "DROP TABLE users;\n")
        self.commit("drop users")
        result = script("door_classify.py", "--json", cwd=self.root)
        self.assertNotIn("measuring nothing", result.stderr)
        self.assertEqual(json.loads(result.stdout)["door"], "one-way")


class PreCommitOneWay(Scratch):
    """C8: without a PR, the pr stage blocks one-way changes on the default branch."""

    def pr_stage(self):
        return script("pr_body_check.py", cwd=self.root)

    def test_uncommitted_one_way_change_on_main_fails(self):
        self.git_repo()
        self.write("migrations/0002_drop.sql", "DROP TABLE users;\n")
        result = self.pr_stage()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(ON_MAIN, result.stdout)

    def test_staged_one_way_change_on_main_fails(self):
        self.git_repo()
        self.write("src/purge.py", "db.run('DELETE FROM users')\n")
        self.git("add", "-A")
        self.assertEqual(self.pr_stage().returncode, 1)

    def test_two_way_change_on_main_is_skipped(self):
        self.git_repo()
        self.write("README.md", "# demo, fixed\n")
        result = self.pr_stage()
        self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("pr: skipped: "), result.stdout)

    def test_one_way_change_on_a_feature_branch_is_skipped(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feature")
        self.write("migrations/0002_drop.sql", "DROP TABLE users;\n")
        self.assertEqual(self.pr_stage().returncode, 78)

    def test_clean_main_with_a_one_way_default_is_skipped(self):
        self.git_repo()
        self.write(".claude/door-rules.yml", "default: one_way\n")
        self.commit("strict")
        self.assertEqual(self.pr_stage().returncode, 78)

    def test_master_without_main_is_the_default_branch(self):
        self.git_repo(branch="master")
        self.write("infra/main.tf", "resource {}\n")
        result = self.pr_stage()
        self.assertEqual(result.returncode, 1)
        self.assertIn("one-way door on master", result.stdout)

    def test_outside_a_repository_is_skipped(self):
        result = self.pr_stage()
        self.assertEqual(result.returncode, 78)
        self.assertTrue(result.stdout.startswith("pr: skipped: "), result.stdout)


class BaseRules(Scratch):
    """F3/F9: a PR cannot lower its own floor; rules come from the base revision too."""

    def feature(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feature")

    def test_weakening_the_rules_in_the_pr_does_not_lower_the_floor(self):
        self.feature()
        weak = "\n".join(line for line in RULES.read_text().splitlines()
                         if "migrations" not in line and "sql" not in line.lower() and "drop" not in line.lower())
        self.write(".claude/door-rules.yml", weak + '\nignore:\n  - "**"\n')
        self.write("migrations/002.sql", "DROP TABLE users;\n")
        self.commit("sneak")
        out = self.classify_json("--base", "main")
        self.assertEqual(out["door"], "one-way")
        self.assertTrue(any("migrations/002.sql" in r for r in out["reasons"]), out)

    def test_deleting_the_rules_in_the_pr_keeps_the_base_rules(self):
        self.feature()
        self.git("rm", "-q", ".claude/door-rules.yml")
        self.write("migrations/002.sql", "DROP TABLE users;\n")
        self.commit("sneak")
        self.assertEqual(self.classify_json("--base", "main")["door"], "one-way")
        self.write("body.md", body(door="two-way"))
        result = script("pr_body_check.py", "--body-file", "body.md", "--base", "main", cwd=self.root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("never lowered", result.stdout)

    def test_rules_added_in_the_pr_still_apply(self):
        self.feature()
        self.write(".claude/door-rules.yml", RULES.read_text().replace("one_way:\n", 'one_way:\n  - "src/core/**"\n', 1))
        self.write("src/core/x.py", "x = 1\n")
        self.commit("tighten")
        self.assertIn("src/core/x.py", " ".join(self.classify_json("--base", "main")["reasons"]))

    def test_the_stricter_default_wins(self):
        self.git_repo(rules=False)
        self.write(".claude/door-rules.yml", "default: one_way\n")
        self.commit("strict")
        self.git("checkout", "-q", "-b", "feature")
        self.write(".claude/door-rules.yml", "default: two_way\n")
        self.write("src/x.py", "x = 1\n")
        self.commit("relax")
        self.assertEqual(self.classify_json("--base", "main")["door"], "one-way")

    def test_rules_missing_everywhere_is_a_clear_error(self):
        self.git_repo(rules=False)
        self.git("checkout", "-q", "-b", "feature")
        self.write("src/x.py", "x = 1\n")
        self.commit("x")
        result = script("door_classify.py", "--base", "main", cwd=self.root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("door-rules.yml", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.write("body.md", body())
        result = script("pr_body_check.py", "--body-file", "body.md", "--base", "main", cwd=self.root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("n/a", result.stdout)
        self.assertIn("door-rules.yml", result.stdout)


class NoBase(Scratch):
    def test_pr_body_without_a_resolvable_base_fails(self):
        self.git_repo(branch="trunk")
        self.write("migrations/002.sql", "DROP TABLE users;\n")
        self.commit("drop")
        self.write("body.md", body())
        result = script("pr_body_check.py", "--body-file", "body.md", cwd=self.root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no base", result.stdout)


class GithubActions(Scratch):
    def test_one_way_pr_is_annotated_and_summarised(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feature")
        self.write("migrations/002.sql", "DROP TABLE users;\n")
        self.commit("drop")
        self.write("body.md", body(door="one-way", extra="Rollback: restore the users backup."))
        summary = self.root / "summary.md"
        env = clean_env(GITHUB_ACTIONS="true", GITHUB_STEP_SUMMARY=str(summary))
        result = script("pr_body_check.py", "--body-file", "body.md", "--base", "main", cwd=self.root, env=env)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("::warning::one-way door: ", result.stdout)
        self.assertIn("one-way door", summary.read_text())

    def test_a_declared_one_way_pr_is_annotated_too(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feature")
        self.write("src/x.py", "x = 1\n")
        self.commit("x")
        self.write("body.md", body(door="one-way", extra="Rollback: revert the merge."))
        env = clean_env(GITHUB_ACTIONS="true")
        result = script("pr_body_check.py", "--body-file", "body.md", "--base", "main", cwd=self.root, env=env)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("::warning::one-way door: ", result.stdout)

    def test_two_way_pr_is_not_annotated(self):
        self.git_repo()
        self.git("checkout", "-q", "-b", "feature")
        self.write("src/x.py", "x = 1\n")
        self.commit("x")
        self.write("body.md", body())
        env = clean_env(GITHUB_ACTIONS="true")
        result = script("pr_body_check.py", "--body-file", "body.md", "--base", "main", cwd=self.root, env=env)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("::warning::", result.stdout)


class KitRules(Scratch):
    def door(self, diff):
        self.write("change.diff", diff)
        return self.classify_json("--rules", str(RULES), "--diff-file", "change.diff")["door"]

    def test_one_way_paths(self):
        for path in ("alembic/versions/0003_drop.py", "db/migrate/20261005_drop.rb", "app/migrate/0001.py",
                     "src/x/auth.py", "src/oauth_client/token.py", "src/middleware/session.py", "payments/charge.py",
                     "src/stripe_api/client.py", ".env", ".env.production", ".github/actions/setup/action.yml",
                     ".github/CODEOWNERS", "CODEOWNERS", ".gitlab-ci.yml", "deploy/main.tf", "docker-compose.yml",
                     "docker-compose.prod.yaml", "Dockerfile.prod", "svc/Dockerfile", "k8s/deploy.yaml",
                     "charts/helm/values.yaml", "CLAUDE.md", "CODING_STANDARDS.md", ".claude/agents/reviewer.md",
                     ".claude/skills/implement-ticket/SKILL.md", "stryker.config.json", "eslint-rules/slopbrake.mjs",
                     ".dependency-cruiser.cjs", "eslint.config.mjs", "prisma/schema.prisma", "db/schema.rb",
                     "app/db/schema.ts", "schema.sql"):
            with self.subTest(path=path):
                self.assertEqual(self.door(new_file_diff(path, "x = 1")), "one-way")

    def test_destructive_content(self):
        for line in ("drop table users;", "ALTER TABLE t DROP COLUMN c;", "alter table t drop constraint k;",
                     "Delete From users", "metadata.drop_all(engine)", "subprocess.run('rm -fr /data')",
                     "from shutil import rmtree; rmtree(p)", "op.drop_column('t', 'c')",
                     "subprocess.run([\"rm\", \"-rf\", path])"):
            with self.subTest(line=line):
                self.assertEqual(self.door(new_file_diff("src/x.py", line)), "one-way")

    def test_false_positives_stay_two_way(self):
        for path, line in (("src/forms/schema.ts", "export const schema = z.object({})"),
                           ("src/x/__tests__/purge.ts", "db.run('DELETE FROM users')"),
                           ("src/purge.spec.ts", "db.run('DELETE FROM users')"),
                           ("src/x/tests/conftest.py", "db.run('DELETE FROM users')"),
                           ("pkg/conftest.py", "db.run('DROP TABLE users')"),
                           ("src/x.py", "    # we never DELETE FROM users here"),
                           ("src/x.ts", "  // DROP TABLE is forbidden")):
            with self.subTest(path=path):
                self.assertEqual(self.door(new_file_diff(path, line)), "two-way")


class RulesLoader(Scratch):
    def run_rules(self, text):
        self.write("rules.yml", text)
        self.write("change.diff", new_file_diff("knex/migrations/1.js", "x"))
        return script("door_classify.py", "--rules", "rules.yml", "--diff-file", "change.diff", cwd=self.root)

    def test_duplicate_top_level_key_is_an_error(self):
        result = self.run_rules('one_way:\n  - "**/migrations/**"\ncontent_patterns: []\none_way:\n  - "x/**"\n')
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate key 'one_way'", result.stderr)

    def test_unknown_key_is_an_error_naming_the_allowed_keys(self):
        result = self.run_rules('One_Way:\n  - "**/migrations/**"\n')
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown key 'One_Way'", result.stderr)
        self.assertIn("one_way, content_patterns, ignore, content_ignore, default", result.stderr)

    def test_malformed_yaml_is_a_clean_error(self):
        result = self.run_rules("one_way:\n  not a list item\n")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)


class BodyShape(Scratch):
    def check(self, text):
        self.write("body.md", text)
        return script("pr_body_check.py", "--body-file", "body.md", "--no-door-floor", cwd=self.root)

    def assert_fails(self, text, message=""):
        result = self.check(text)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(message, result.stdout)

    def test_door_hidden_from_the_reader_does_not_count(self):
        for hidden in ("<!-- **Door:** two-way -->", "`**Door:** two-way`", "   ```\n   **Door:** two-way\n   ```"):
            with self.subTest(hidden=hidden):
                self.assert_fails(body().replace("**Door:** two-way", hidden), "exactly one '**Door:**")

    def test_an_example_in_an_indented_fence_is_not_a_second_door(self):
        extra = "   ```\n   **Door:** one-way\n   ```"
        self.assertEqual(self.check(body(extra=extra)).returncode, 0, self.check(body(extra=extra)).stdout)

    def test_door_must_be_exactly_one_way_or_two_way(self):
        self.assert_fails(body(door="two-way-ish"), "exactly one '**Door:**")

    def test_blast_radius_needs_a_word(self):
        self.assert_fails(body(radius="```"), "Blast Radius")
        self.assert_fails(body(radius="<!-- db -->"), "Blast Radius")

    def test_blast_radius_label_is_case_insensitive(self):
        self.assertEqual(self.check(body().replace("**Blast Radius:**", "**Blast radius:**")).returncode, 0)

    def test_rollback_plans_that_say_nothing_fail(self):
        for extra in ("No rollback possible.", "Mitigation: none.", "**Rollback:** n/a", "Rollback plan: N/A", "Mitigations: none"):
            with self.subTest(extra=extra):
                self.assert_fails(body(door="one-way", extra=extra), "rollback")

    def test_real_rollback_plans_pass(self):
        for extra in ("Rollback: restore from backup.", "**Rollback plan:**\n- revert the merge and run 0002 down",
                      "Mitigation: feature flag `new_billing` stays off until verified."):
            with self.subTest(extra=extra):
                result = self.check(body(door="one-way", extra=extra))
                self.assertEqual(result.returncode, 0, result.stdout)


class ReviewTraceability(Scratch):
    def setUp(self):
        super().setUp()
        self.git_repo()
        self.git("checkout", "-q", "-b", "feature")
        self.write("src/x.py", "total = 1\n")
        self.commit("add total")
        self.write("src/x.py", "grand_total = 1\n")
        self.commit("review: rename total (naming)")

    def check(self, log):
        self.write("body.md", body(log=log))
        return script("pr_body_check.py", "--body-file", "body.md", "--base", "main", cwd=self.root)

    def test_unlogged_review_commit_fails_naming_it(self):
        result = self.check("- none")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("review: rename total (naming)", result.stdout)

    def test_review_commit_logged_by_finding_passes(self):
        result = self.check("- rename total (naming) → pending")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_review_commit_logged_by_sha_passes(self):
        sha = self.git("rev-parse", "--short=9", "HEAD")
        result = self.check(f"- renamed the counter → {sha}")
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
