"""Round 2 door decisions: B10 (rules, C8 with a PR body), B16 (default branch), B17 (G3 steps, auth globs),
and C8 judging the staged commit rather than the working tree."""
import json
import shutil
import sys
import unittest

from test_doors import (
    GIT_ID,
    GUARDS,
    ON_MAIN,
    RULES,
    Scratch,
    body,
    clean_env,
    new_file_diff,
    script,
    sh,
)

sys.path.insert(0, str(GUARDS))
from pr_body_check import has_rollback_plan


class R2Scratch(Scratch):
    def setUp(self):
        super().setUp()
        self.gitconfig = self.write("../gitconfig", "")  # no operator init.defaultBranch leaks in

    def env(self, **extra):
        return clean_env(GIT_CONFIG_GLOBAL=str(self.gitconfig), GIT_CONFIG_NOSYSTEM="1", **extra)

    def door(self, path, *lines):
        self.write("change.diff", new_file_diff(path, *lines))
        result = script("door_classify.py", "--json", "--rules", str(RULES), "--diff-file", "change.diff",
                        cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["door"]

    def pr_stage(self, **extra):
        return script("pr_body_check.py", cwd=self.root, env=self.env(**extra))


class B10Rules(R2Scratch):
    def test_gate_driving_configs_and_the_marker_are_one_way(self):
        for path in (".claude/slopbrake.json", "ruff.toml", ".ruff.toml", "pytest.ini", "tox.ini", "setup.cfg",
                     "mypy.ini", ".mypy.ini", "pyrightconfig.json", "vitest.config.ts", "jest.config.js",
                     "tsconfig.json", "tsconfig.build.json"):
            with self.subTest(path=path):
                self.assertEqual(self.door(path, "x = 1"), "one-way")

    def test_deleting_the_marker_on_main_is_blocked(self):
        self.git_repo()
        self.write(".claude/slopbrake.json", "{}\n")
        self.commit("marker")
        self.git("rm", "-q", ".claude/slopbrake.json")
        result = self.pr_stage()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("deletes .claude/slopbrake.json", result.stdout)

    def test_gate_scripts_and_tool_tables_are_one_way(self):
        for path, line in (("package.json", '    "test": "true",'), ("package.json", '  "lint" : "true"'),
                           ("package.json", '    "typecheck": "true",'), ("package.json", '    "check": "true",'),
                           ("package.json", '    "smoke": "true",'), ("pyproject.toml", "[tool.ruff]"),
                           ("pyproject.toml", "[tool.pytest.ini_options]"), ("pyproject.toml", "[tool.mypy]"),
                           ("pyproject.toml", "[tool.pyright]"), ("pyproject.toml", "[tool.coverage.run]")):
            with self.subTest(path=path, line=line):
                self.assertEqual(self.door(path, line), "one-way")

    def test_ordinary_manifest_edits_stay_two_way(self):
        for path, line in (("package.json", '  "version": "1.2.0",'), ("package.json", '    "build": "tsc",'),
                           ("pyproject.toml", "[project]"), ("pyproject.toml", 'version = "1.2.0"')):
            with self.subTest(path=path, line=line):
                self.assertEqual(self.door(path, line), "two-way")

    def test_docs_are_not_classified(self):
        for path in ("docs/auth/README.md", "docs/payments/flow.md", "docs/migrations/0001.md"):
            with self.subTest(path=path):
                self.assertEqual(self.door(path, "DROP TABLE users;"), "two-way")

    def test_a_docs_only_commit_on_main_passes_the_pr_stage(self):
        self.git_repo()
        self.write("docs/auth/README.md", "# Auth: typo fix\n")
        self.git("add", "-A")
        self.assertEqual(self.pr_stage().returncode, 78)

    def test_removing_build_outputs_is_two_way(self):
        for line in ('    "clean": "rm -rf dist",', "\trm -rf build", "rm -rf ./build/", "rm -rf dist build coverage",
                     "rm -rf node_modules .next .cache", "rm -rf out tmp && npm run build", "rm -fr -- dist",
                     "subprocess.run('rm -rf dist', shell=True)"):
            with self.subTest(line=line):
                self.assertEqual(self.door("Makefile", line), "two-way")

    def test_removing_anything_else_stays_one_way(self):
        for line in ("rm -rf dist ~/data", "rm -rf $OUT", "rm -rf dist && rm -rf /srv", "rm -rf /tmp/x",
                     "rm -rf dist/*", "rm -rf build-cache", "rm -rf distribution", "rm -rf ../dist",
                     'rm -rf "$DATA_DIR"', "rm -rf '/var/lib/app'", 'rm -rf "/data"', 'rm -rf "$HOME"/uploads',
                     'rm -rf -- "$1"', "rm -rf `cat dirs`", 'os.system("rm -rf " + user_dir)',
                     'rm -rf dist "$DATA_DIR"', "os.system('rm -rf dist ' + user_dir)"):
            with self.subTest(line=line):
                self.assertEqual(self.door("Makefile", line), "one-way")


    def test_a_trailing_comment_keeps_the_build_output_exemption(self):
        for line in ("rm -rf dist # clean", "rm -rf build node_modules  # fresh install"):
            with self.subTest(line=line):
                self.assertEqual(self.door("Makefile", line), "two-way")

    def test_a_file_renamed_out_of_docs_is_classified(self):
        self.git_repo()
        self.write("docs/drafts/0003_drop.sql", "DROP TABLE users;\n")
        self.commit("draft")
        (self.root / "db/migrations").mkdir(parents=True)
        self.git("mv", "docs/drafts/0003_drop.sql", "db/migrations/0003_drop.sql")
        result = self.pr_stage()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("db/migrations/0003_drop.sql", result.stdout)
        self.commit("move")
        result = script("door_classify.py", "--json", "--base", "HEAD~1", cwd=self.root, env=self.env())
        self.assertEqual(json.loads(result.stdout)["door"], "one-way", result.stdout)

    def test_a_file_renamed_into_docs_still_counts_its_old_path(self):
        self.git_repo()
        self.write(".env.production", "KEY=1\n")
        self.commit("env")
        (self.root / "docs").mkdir()
        self.git("mv", ".env.production", "docs/env.txt")
        self.assertEqual(self.pr_stage().returncode, 1)


class C8WithABody(R2Scratch):
    """B10: C8 applies on the default branch even when a PR body is supplied."""

    def setUp(self):
        super().setUp()
        self.git_repo()
        self.write("db/migrations/010_drop.sql", "DROP TABLE payments;\n")
        self.git("add", "-A")
        self.write("../body.md", body(door="two-way"))

    def test_pr_body_file_does_not_skip_c8(self):
        result = self.pr_stage(PR_BODY_FILE=str(self.root / "../body.md"))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(ON_MAIN, result.stdout)

    def test_a_pull_request_event_does_not_skip_c8(self):
        event = self.write("../event.json", json.dumps({"pull_request": {"body": body(door="two-way")}}))
        result = self.pr_stage(GITHUB_EVENT_PATH=str(event))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(ON_MAIN, result.stdout)

    def test_a_body_with_a_clean_main_still_checks_the_body(self):
        self.git("reset", "-q", "--hard")
        self.git("clean", "-fdq")
        result = self.pr_stage(PR_BODY_FILE=str(self.root / "../body.md"))
        self.assertNotIn("one-way door on main", result.stdout)
        self.assertIn("pr-body: ", result.stdout)


class C8JudgesTheCommit(R2Scratch):
    """At pre-commit the staged diff is what lands, so C8 classifies it."""

    def test_staged_then_deleted_from_disk_is_blocked(self):
        self.git_repo()
        self.write("db/migrations/009_drop.sql", "DROP TABLE accounts;\n")
        self.git("add", "-A")
        (self.root / "db/migrations/009_drop.sql").unlink()
        result = self.pr_stage()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(ON_MAIN, result.stdout)

    def test_a_staged_hunk_reverted_on_disk_is_blocked(self):
        self.git_repo()
        self.write("src/purge.py", "x = 1\n")
        self.commit("purge")
        self.write("src/purge.py", "x = 1\ndb.run('DELETE FROM users')\n")
        self.git("add", "-A")
        self.write("src/purge.py", "x = 1\n")
        self.assertEqual(self.pr_stage().returncode, 1)

    def test_an_unstaged_one_way_file_does_not_block_a_two_way_commit(self):
        self.git_repo()
        self.write("README.md", "# demo, fixed\n")
        self.git("add", "-A")
        self.write("db/migrations/011_drop.sql", "DROP TABLE users;\n")  # not part of this commit
        result = self.pr_stage()
        self.assertEqual(result.returncode, 78, result.stdout)
        self.assertIn("staged change", result.stdout)

    def test_without_anything_staged_the_working_tree_is_judged(self):
        self.git_repo()
        self.write("db/migrations/011_drop.sql", "DROP TABLE users;\n")
        self.assertEqual(self.pr_stage().returncode, 1)


    def test_an_unstaged_tracked_one_way_edit_blocks_while_something_is_staged(self):
        # run-stages.sh unsets GIT_INDEX_FILE, so `commit -a` / `commit -- <path>` content may still be unstaged
        self.git_repo()
        self.write("db/schema.sql", "CREATE TABLE users (id int);\n")
        self.commit("schema")
        self.write("README.md", "# demo, fixed\n")
        self.git("add", "README.md")
        self.write("db/schema.sql", "CREATE TABLE users (id int);\nDROP TABLE users;\n")
        result = self.pr_stage()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("db/schema.sql", result.stdout)

    def test_a_hook_index_with_nothing_staged_says_so(self):
        self.git_repo()
        index = self.git("rev-parse", "--git-path", "index")
        result = self.pr_stage(GIT_INDEX_FILE=str(self.root / index))
        self.assertEqual(result.returncode, 78, result.stdout)
        self.assertIn("nothing staged", result.stdout)


class C8ThroughTheRealHook(R2Scratch):
    """The pre-commit hook runs scripts/check, whose runner unsets GIT_INDEX_FILE."""

    def setUp(self):
        super().setUp()
        self.git_repo()
        shutil.copytree(GUARDS, self.root / "scripts/slopbrake", ignore=shutil.ignore_patterns("__pycache__"))
        self.write("scripts/check", """\
            #!/usr/bin/env bash
            cd "$(dirname "$0")/.."
            source scripts/slopbrake/run-stages.sh
            STAGES=(pr)
            stage_pr() { python3 scripts/slopbrake/pr_body_check.py; }
            run_stages "$@"
            """)
        self.write(".githooks/pre-commit", "#!/usr/bin/env bash\nexec scripts/check --fast\n")
        for rel in ("scripts/check", ".githooks/pre-commit"):
            (self.root / rel).chmod(0o755)
        self.git("config", "core.hooksPath", ".githooks")
        self.write("db/schema.sql", "CREATE TABLE users (id int);\n")
        self.commit("gate and schema")
        self.write("README.md", "# demo, fixed\n")
        self.git("add", "README.md")
        self.write("db/schema.sql", "CREATE TABLE users (id int);\nDROP TABLE users;\n")

    def hook_commit(self, *args):
        head = self.git("rev-parse", "HEAD")
        result = sh(["git", *GIT_ID, "commit", "-q", *args], self.root, self.env())
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertIn("one-way door on main", result.stdout + result.stderr)

    def test_commit_all_is_blocked(self):
        self.hook_commit("-a", "-m", "sneaky")

    def test_commit_only_a_path_is_blocked(self):
        self.hook_commit("-m", "sneaky", "--", "db/schema.sql")


class B16DefaultBranch(R2Scratch):
    def default_branch(self):
        code = f"import sys; sys.path.insert(0, {str(GUARDS)!r}); import common; print(common.default_branch())"
        result = sh([sys.executable, "-c", code], self.root, self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def configure(self, name):
        self.gitconfig.write_text(f"[init]\n\tdefaultBranch = {name}\n")

    def test_origin_head_wins(self):
        self.git_repo()
        self.git("branch", "develop")
        self.git("update-ref", "refs/remotes/origin/develop", "HEAD")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
        self.configure("main")
        self.assertEqual(self.default_branch(), "develop")

    def test_init_default_branch_when_it_exists_beats_main(self):
        self.git_repo()
        self.git("branch", "trunk")
        self.configure("trunk")
        self.assertEqual(self.default_branch(), "trunk")

    def test_init_default_branch_that_does_not_exist_falls_back_to_main_then_master(self):
        self.git_repo()
        self.configure("trunk")
        self.assertEqual(self.default_branch(), "main")
        self.git("branch", "-m", "master")
        self.assertEqual(self.default_branch(), "master")

    def test_no_known_default_is_none_even_with_a_single_branch(self):
        self.git_repo(branch="trunk")
        self.assertEqual(self.default_branch(), "None")

    def test_c8_uses_it_on_a_configured_trunk_with_other_branches(self):
        self.git_repo(branch="trunk")
        self.git("branch", "feat")
        self.configure("trunk")
        self.write("migrations/001.sql", "DROP TABLE accounts;\n")
        result = self.pr_stage()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("one-way door on trunk", result.stdout)


class B17Steps(unittest.TestCase):
    def test_roll_back_undeploy_and_make_rollback_are_steps(self):
        for text in ("Rollback: roll back to v1.2.3 via the deploy dashboard.", "Rollback: roll-back the release.",
                     "Rollback plan: roll back the deployment in Argo.", "Rollback: undeploy the new service.",
                     "Mitigation: undeploy the worker.", "Rollback: make rollback", "Rollback: run `make rollback`.",
                     "**Rollback:**\n- roll back to the previous tag"):
            with self.subTest(text=text):
                self.assertTrue(has_rollback_plan(text))

    def test_negated_roll_back_is_not_a_step(self):
        for text in ("Rollback: no roll back possible.", "There is no way to roll back.",
                     "Rollback: none; roll back is impossible.", "Rollback: we cannot undeploy this.",
                     "Rollback: none.", "No rollback.", "This is irreversible; nobody can roll back."):
            with self.subTest(text=text):
                self.assertFalse(has_rollback_plan(text))


    def test_undeploy_is_a_step_like_roll_back(self):
        self.assertTrue(has_rollback_plan("undeploy the service"))
        self.assertTrue(has_rollback_plan("roll back the service"))


class B17AuthGlobs(R2Scratch):
    def test_auth_directories_are_one_way(self):
        for path in ("src/auth/x.py", "src/oauth/token.py", "src/user-auth/x.py", "src/auth_service/x.py",
                     "src/auth-server/x.ts", "src/x/auth.py", "src/Auth/x.ts", "lib/middleware/auth/session.ts"):
            with self.subTest(path=path):
                self.assertEqual(self.door(path, "x = 1"), "one-way")

    def test_author_directories_are_two_way(self):
        for path in ("src/authors/list.py", "src/blog/authoring/editor.ts", "src/authority/x.py", "docs/author.md"):
            with self.subTest(path=path):
                self.assertEqual(self.door(path, "x = 1"), "two-way")


if __name__ == "__main__":
    unittest.main()
