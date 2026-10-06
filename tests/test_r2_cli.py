"""Round-2 installer decisions (FIX-SPEC B1, B6-B9, B12, B13, B15, B16, B18) through the command line,
cli's public functions and the TypeScript scripts/check template."""
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
import unittest.mock
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))

from slopbrake import cli, hooks

GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]
KIT_COMMON = HOME / "slopbrake/kit/common/scripts/slopbrake"
SCRUBBED = ("GITHUB_BASE_REF", "GITHUB_EVENT_PATH", "PR_BODY_FILE", "SLOPBRAKE_BASE")


def sh(cmd, cwd, env=None, stdin=None):
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "repo"
        self.root.mkdir()
        self.tmpdir = base / "tmp"  # TMPDIR for the cli, to catch leaked scratch dirs
        self.home = base / "home"  # HOME and XDG dirs: never the user's real settings or trust list
        for d in (self.tmpdir, self.home, base / "config", base / "state"):
            d.mkdir()
        self.env = {"PYTHONPATH": str(HOME), "TMPDIR": str(self.tmpdir), "HOME": str(self.home),
                    "XDG_CONFIG_HOME": str(base / "config"), "XDG_STATE_HOME": str(base / "state"),
                    # the real tool cache, so the gate's `uvx ruff` does not download again under the fake HOME
                    "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv"))}

    def write(self, rel, text, base=None):
        path = (base or self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def git(self, *args, path=None):
        return sh(["git", *args], path or self.root).stdout.strip()

    def git_repo(self, path=None, branch="main"):
        path = path or self.root
        sh(["git", "init", "-q", "-b", branch], path)
        self.write("README.md", "# demo\n", base=path)
        self.commit("init", path)

    def commit(self, message, path=None):
        sh(["git", "add", "-A"], path or self.root)
        result = sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", message], path or self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def gf(self, *args, cwd=None, **env):
        full = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
        full.update(self.env, **env)
        return sh([sys.executable, "-m", "slopbrake", *args], cwd or self.root, env=full)

    def python_repo(self, path=None):
        path = path or self.root
        self.git_repo(path)
        self.write("pyproject.toml", "[project]\nname = 'shop'\n", base=path)
        self.write("shop.py", "def one():\n    return 1\n", base=path)
        self.write("tests/test_shop.py", "import unittest\nfrom shop import one\n\n\nclass T(unittest.TestCase):\n"
                                         "    def test_one(self):\n        self.assertEqual(one(), 1)\n", base=path)
        self.commit("app", path)

    def installed(self):
        self.python_repo()
        result = self.gf("init", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.commit("install slopbrake")

    def status(self, repo=None):
        result = self.gf("status", str(repo or self.root), "--json")
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        return json.loads(result.stdout)[0]

    def init_json(self, *extra):
        result = self.gf("init", str(self.root), "--json", *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def assert_usage_error(self, result, text):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn(text, result.stderr)

    def refs(self):
        return self.git("for-each-ref", "--format=%(refname)", "refs/slopbrake").split()


# ── B1: the encoded ratchet ref ──────────────────────────────────────────────

class RatchetRef(Scratch):
    def test_feature_and_feature_slash_x_get_their_own_ratchets(self):
        self.python_repo()
        sh(["git", "switch", "-q", "-c", "feature"], self.root)
        self.assertEqual(self.gf("init", str(self.root)).returncode, 0)
        sh(["git", "switch", "-q", "main"], self.root)
        sh(["git", "branch", "-q", "-D", "feature"], self.root)  # its ratchet stays behind
        self.assertEqual(sh(["git", "switch", "-q", "-c", "feature/x"], self.root).returncode, 0)
        result = self.gf("init", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refs/slopbrake/last-green/feature", self.refs())
        self.assertIn("refs/slopbrake/last-green/feature%2Fx", self.refs())

    def test_the_helper_encodes_every_slash(self):
        self.assertEqual(cli.ratchet_ref("feature/x/y"), "refs/slopbrake/last-green/feature%2Fx%2Fy")
        self.assertEqual(cli.ratchet_ref("main"), "refs/slopbrake/last-green/main")

    def test_an_old_nested_ratchet_does_not_block_the_branch_ref(self):
        self.python_repo()
        sh(["git", "update-ref", "refs/slopbrake/last-green/main/old", "HEAD"], self.root)  # pre-B1 layout
        result = self.gf("init", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.refs(), ["refs/slopbrake/last-green/main"])


# ── B6: init leaves the interpreter to scripts/check ─────────────────────────

class TestArgs(Scratch):
    def test_test_args_carry_no_interpreter(self):
        self.write("pyproject.toml", "[tool.pytest.ini_options]\n")
        (self.root / ".venv/bin").mkdir(parents=True)
        self.assertEqual(cli.substitutions(self.root, "python")["TEST_ARGS"], "-m pytest -q")
        self.write("uv.lock", "")
        self.assertEqual(cli.substitutions(self.root, "python")["TEST_ARGS"], "-m pytest -q")
        with tempfile.TemporaryDirectory() as tmp:
            self.write("pyproject.toml", "[project]\nname='x'\n", base=Path(tmp))
            self.assertEqual(cli.substitutions(Path(tmp), "python")["TEST_ARGS"], "-m unittest discover -s tests")

    def test_no_substitution_bakes_in_a_checkout_path(self):
        self.write("pyproject.toml", "[tool.pytest.ini_options]\n")
        (self.root / ".venv/bin").mkdir(parents=True)
        for key, value in cli.substitutions(self.root, "python").items():
            if key != "CI_INSTALL":  # CI creates its own .venv
                self.assertNotIn("$PWD", value, key)
                self.assertNotIn(".venv", value, key)

    def test_the_placeholder_is_filled_in_the_rendered_gate(self):
        self.write("pyproject.toml", "[tool.pytest.ini_options]\n")
        text = cli.render("TEST_CMD=\"$PY @TEST_ARGS@\"\n", cli.substitutions(self.root, "python"))
        self.assertEqual(text, 'TEST_CMD="$PY -m pytest -q"\n')


# ── B7: TypeScript layouts ───────────────────────────────────────────────────

class TypescriptLayouts(Scratch):
    def subs(self, *dirs):
        for d in dirs:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.write("package.json", "{}")
        return cli.substitutions(self.root, "typescript")

    def test_packages_src_monorepo_and_a_test_dir_are_covered(self):
        subs = self.subs("packages/a/src", "packages/b/src", "test")
        self.assertEqual(subs["MUTATE_INCLUDE"], "--include 'packages/*/src/**/*.ts' --include 'packages/*/src/**/*.tsx'")
        for glob in ("'test/**/*.test.ts'", "'test/**/*.spec.tsx'", "'packages/*/src/**/*.test.ts'"):
            self.assertIn(glob, subs["TEST_GLOBS"])
        self.assertNotIn("'tests/", subs["TEST_GLOBS"])
        self.assertEqual(subs["DEPCRUISE_PATHS"], "packages test")
        self.assertEqual(subs["PACKAGES_ROOT"], "packages/[^/]+/src")

    def test_src_and_lib_are_both_mutated_and_both_package_roots(self):
        subs = self.subs("src", "lib", "__tests__")
        self.assertIn("--include 'src/**/*.ts'", subs["MUTATE_INCLUDE"])
        self.assertIn("--include 'lib/**/*.tsx'", subs["MUTATE_INCLUDE"])
        self.assertIn("'__tests__/**/*.test.ts'", subs["TEST_GLOBS"])
        self.assertIn("'lib/**/*.spec.ts'", subs["TEST_GLOBS"])
        self.assertEqual(subs["DEPCRUISE_PATHS"], "src lib __tests__")
        self.assertEqual(subs["PACKAGES_ROOT"], "(?:src|lib)")

    def test_lib_only_package(self):
        subs = self.subs("lib", "tests")
        self.assertEqual(subs["MUTATE_INCLUDE"], "--include 'lib/**/*.ts' --include 'lib/**/*.tsx'")
        self.assertEqual(subs["PACKAGES_ROOT"], "lib")
        self.assertEqual(subs["DEPCRUISE_PATHS"], "lib tests")

    def test_package_glob_matches_the_way_changed_ranges_reads_it(self):
        sys.path.insert(0, str(KIT_COMMON))
        from common import glob_to_regex
        self.assertTrue(glob_to_regex("packages/*/src/**/*.ts").match("packages/a/src/x.ts"))

    def ts_repo(self, *files):
        self.git_repo()
        self.write("package.json", '{"name": "t", "scripts": {"typecheck": "tsc --noEmit"}}')
        self.write("tsconfig.json", "{}")
        self.write("eslint.config.mjs", "import slopbrake from './eslint-rules/slopbrake.mjs';\nexport default [];\n")
        for rel in files:
            self.write(rel, "export const x = 1;\n")
        result = self.gf("init", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.commit("install slopbrake")

    def test_status_reports_a_typescript_dir_outside_every_gate_glob(self):
        self.ts_repo("src/core/index.ts", "tests/core.test.ts", "vite.config.ts")
        self.assertFalse(any("outside" in gap for gap in self.status()["gaps"]), self.status()["gaps"])
        self.write("app/main.ts", "export const app = 1;\n")
        self.write("types/env.d.ts", "declare const x: number;\n")
        sh(["git", "add", "-A"], self.root)  # coverage counts tracked sources only
        gaps = [gap for gap in self.status()["gaps"] if "outside" in gap]
        self.assertEqual(len(gaps), 1, self.status()["gaps"])
        self.assertIn("app/", gaps[0])
        self.assertNotIn("types/", gaps[0])

    def test_status_is_quiet_for_a_monorepo_init_covered(self):
        self.ts_repo("packages/a/src/index.ts", "packages/a/src/lib/x.ts", "test/x.test.ts")
        self.assertFalse(any("outside" in gap for gap in self.status()["gaps"]), self.status()["gaps"])


# ── B7 / B18: the TypeScript scripts/check template ──────────────────────────

FAKE_TOOL = '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$ARGV_DIR/$(basename "$0")"\nexit 0\n'
FAKE_PM = ('#!/usr/bin/env bash\necho "$(basename "$0") $*" >> "$ARGV_DIR/pm.log"\n'
           'if [ "$1 $2" = "run -s" ]; then echo "ran $3"; fi\n')


class TypescriptGate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.argv, self.bin = (Path(tmp.name) / d for d in ("repo", "argv", "bin"))
        self.argv.mkdir()
        self.bin.mkdir()
        self.exe(self.bin / "npm", FAKE_PM)
        shutil.copytree(KIT_COMMON, self.root / "scripts/slopbrake", ignore=shutil.ignore_patterns("__pycache__"))
        subs = {"PM": "npm", "DEPCRUISE_PATHS": "src tests", "TEST_GLOBS": "'tests/**/*.test.ts'",
                "MUTATE_INCLUDE": "--include 'src/**/*.ts'"}
        self.exe(self.root / "scripts/check", cli.render((cli.KIT / "typescript/scripts/check").read_text(), subs))
        self.write("package.json", '{"name": "t", "scripts": {}}\n')
        self.write("src/a.ts", "export const a = 1;\n")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", "init"], cwd=self.root, check=True)

    def exe(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def check(self, *stages, **env):
        full = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
        full.update(PATH=f"{self.bin}:{os.environ['PATH']}", ARGV_DIR=str(self.argv), SLOPBRAKE_NO_RECORD="1", **env)
        result = subprocess.run([str(self.root / "scripts/check"), *stages], cwd=self.root, env=full,
                                capture_output=True, text=True, timeout=60, check=False)
        result.out = result.stdout + result.stderr
        return result

    def test_missing_lint_script_skips_with_a_reason(self):
        result = self.check("lint")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn('lint: skipped: no "lint" script in package.json', result.out)
        self.assertRegex(result.out, r"(?m)^skip +lint\b")

    def test_missing_test_script_fails_saying_so(self):
        result = self.check("tests")
        self.assertEqual(result.returncode, 1, result.out)
        self.assertIn("no test script in package.json", result.out)

    def test_present_scripts_run(self):
        self.write("package.json", '{"scripts": {"lint": "eslint .", "test": "vitest run"}}\n')
        result = self.check("lint", "tests")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("ran lint", result.out)
        self.assertIn("ran test", result.out)

    def test_mutation_prints_the_base_it_used(self):
        self.exe(self.root / "node_modules/.bin/stryker", FAKE_TOOL)
        self.write("src/a.ts", "export const a = 2;\n")
        result = self.check("mutation", SLOPBRAKE_BASE="HEAD")
        self.assertIn("mutation: base ", result.out)
        self.assertTrue((self.argv / "stryker").is_file(), result.out)

    def test_mutation_without_changes_names_the_base_too(self):
        result = self.check("mutation", SLOPBRAKE_BASE="HEAD")
        self.assertIn("mutation: base ", result.out)
        self.assertIn("no changed source lines", result.out)

    def test_no_dead_skip_branch_in_the_mutation_stage(self):
        stage = (cli.KIT / "typescript/scripts/check").read_text().split("stage_mutation()", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("-eq 78", stage)


# ── B8: repository roots only ────────────────────────────────────────────────

class SubdirectoryInstalls(Scratch):
    MESSAGE = "monorepo subdirectories are not supported yet"

    def test_init_status_and_verify_refuse_a_subdirectory(self):
        self.git_repo()
        app = self.root / "packages/app"
        self.write("packages/app/pyproject.toml", "[project]\nname = 'app'\n")
        result = self.gf("init", str(app))
        self.assert_usage_error(result, f"slopbrake installs at a repository root ({self.root.resolve()}); "
                                + self.MESSAGE)
        self.assertFalse((app / "scripts").exists())
        self.assertEqual(self.git("config", "--get", "core.hooksPath"), "")
        self.assert_usage_error(self.gf("status", str(app)), self.MESSAGE)
        self.assert_usage_error(self.gf("verify", str(app)), self.MESSAGE)
        self.assertEqual(list(self.tmpdir.iterdir()), [])


# ── B9 / B13: status gaps ────────────────────────────────────────────────────

class StatusGaps(Scratch):
    def gaps(self):
        return "\n".join(self.status()["gaps"])

    def test_foreign_git_hooks_that_never_run_the_gate_are_a_gap(self):
        self.installed()
        for name in ("pre-commit", "pre-push"):
            self.write(f".husky/{name}", "#!/bin/sh\necho lint-staged\n").chmod(0o755)
        sh(["git", "config", "core.hooksPath", ".husky"], self.root)
        self.assertIn("do not run scripts/check", self.gaps())
        self.assertTrue(any("do not run scripts/check" in g for g in cli.wiring_gaps(self.root)))  # verify's H5/H6

    def test_hooks_that_chain_the_gate_are_fine(self):
        self.installed()
        self.write(".husky/pre-commit", "#!/bin/sh\nscripts/check --fast\n")
        self.write(".husky/pre-push", "#!/bin/sh\nscripts/check\n")
        self.write(".husky/_/pre-commit", '#!/bin/sh\n. "$(dirname "$0")/h"\n').chmod(0o755)  # husky v9 stubs
        self.write(".husky/_/pre-push", '#!/bin/sh\n. "$(dirname "$0")/h"\n').chmod(0o755)
        sh(["git", "config", "core.hooksPath", ".husky/_"], self.root)
        self.assertNotIn("do not run scripts/check", self.gaps())

    def test_door_rules_missing_kit_rules_are_counted(self):
        self.installed()
        kit = cli.door_rule_items((cli.KIT / "common/.claude/door-rules.yml").read_text())
        self.assertIn(("one_way", "**/migrations/**"), kit)  # the parser reads quoted globs...
        self.assertTrue(any(key == "content_patterns" and "\\b" in item for key, item in kit))  # ...and regexes
        old = self.write(".claude/door-rules.yml", 'one_way:\n  - "**/migrations/**"  # data\n  - CODEOWNERS\n'
                                                   '\ncontent_patterns:\n  - "sendEmail\\\\("\n')
        self.assertEqual(cli.door_rule_items(old.read_text()),
                         {("one_way", "**/migrations/**"), ("one_way", "CODEOWNERS"), ("content_patterns", "sendEmail\\\\(")})
        gaps = [g for g in self.status()["gaps"] if "door-rules.yml" in g]
        self.assertEqual(len(gaps), 1, self.status()["gaps"])
        self.assertIn("merge them by hand", gaps[0])
        self.assertIn(f"lacks {len(kit - cli.door_rule_items(old.read_text()))} kit rule", gaps[0])

    def test_env_overridable_floor_or_tool_exec_is_a_gap(self):
        self.installed()
        check = self.root / "scripts/check"
        check.write_text(check.read_text().replace("export MUTATION_FLOOR=60",
                                                   'export MUTATION_FLOOR="${MUTATION_FLOOR:-60}"'))
        self.assertIn("MUTATION_FLOOR from the environment", self.gaps())
        check.write_text(check.read_text() + "# never `$PM exec` here\nstage_x() { $PM exec eslint .; }\n")
        self.assertIn("hand-merge the kit's scripts/check", self.gaps())
        self.assertEqual(sum("exec" in g for g in self.status()["gaps"]), 1)

    def test_user_hooks_not_installed_is_a_note_not_a_gap(self):
        self.installed()
        result = self.status()
        self.assertTrue(any("user-level hooks are not installed" in n for n in result["notes"]), result)
        self.assertFalse(any("user-level" in g for g in result["gaps"]))
        human = self.gf("status", str(self.root)).stdout
        self.assertIn("user-level hooks are not installed", human)

    def test_user_level_disable_all_hooks_is_a_gap(self):
        self.installed()
        self.write(".claude/settings.json", '{"disableAllHooks": true}\n', base=self.home)
        self.assertIn("disableAllHooks", self.gaps())

    def test_fresh_install_has_none_of_these_gaps(self):
        self.installed()
        self.assertEqual(self.status()["gaps"], ["types stage is not configured: choose a type checker in scripts/check"])

    def test_typescript_without_a_typecheck_script_is_a_gap(self):
        self.git_repo()
        self.write("package.json", '{"name": "t", "scripts": {"lint": "eslint ."}}')
        self.write("tsconfig.json", "{}")
        self.write("src/core/index.ts", "export const x = 1;\n")
        self.write("eslint.config.mjs", "import slopbrake from './eslint-rules/slopbrake.mjs';\nexport default [];\n")
        self.assertEqual(self.gf("init", str(self.root)).returncode, 0)
        self.assertIn('package.json has no "typecheck" script', self.gaps())
        self.write("package.json", '{"name": "t", "scripts": {"typecheck": "tsc --noEmit"}}')
        self.assertNotIn("typecheck", self.gaps())


# ── B12 / B18: init next steps ───────────────────────────────────────────────

class NextSteps(Scratch):
    def steps(self):
        return self.init_json()["next_steps"]

    def test_no_tests_yet_gets_a_hint_before_the_commit_step(self):
        self.git_repo()
        self.write("pyproject.toml", "[project]\nname = 'shop'\n")
        (self.root / "tests").mkdir()
        steps = self.steps()
        hint = next(i for i, s in enumerate(steps) if "add a first test" in s)
        self.assertLess(hint, next(i for i, s in enumerate(steps) if "git commit" in s))

    def test_a_repo_with_tests_gets_no_test_hint(self):
        self.python_repo()
        self.assertFalse(any("add a first test" in s for s in self.steps()))

    def test_pytest_without_venv_or_uv_lock_gets_a_hint(self):
        self.python_repo()
        self.write("pyproject.toml", "[project]\nname = 'shop'\n[tool.pytest.ini_options]\n")
        self.assertTrue(any("pytest" in s and ".venv" in s for s in self.steps()))
        (self.root / ".venv").mkdir()
        self.assertFalse(any("pytest" in s and ".venv" in s for s in self.steps()))

    def test_no_commits_yet_commits_on_the_current_branch(self):
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("pyproject.toml", "[project]\nname = 'shop'\n")
        self.write("tests/test_x.py", "")
        steps = self.steps()
        self.assertFalse(any("git switch -c" in s or "one-way" in s for s in steps), steps)
        self.assertTrue(any("git commit" in s and "main" in s for s in steps), steps)

    def test_typescript_without_a_typecheck_script_gets_a_hint(self):
        self.git_repo()
        self.write("package.json", '{"name": "t"}')
        self.write("tsconfig.json", "{}")
        self.assertTrue(any('"typecheck"' in s for s in self.steps()))

    @unittest.skipUnless(hasattr(hooks, "trust"), "hooks.trust lands with the hooks worker (B11 b)")
    def test_init_trusts_the_repo_for_the_user_level_stop_gate(self):
        self.python_repo()
        self.init_json()
        trusted = Path(self.env["XDG_CONFIG_HOME"]) / "slopbrake/trusted.json"
        self.assertIn(str(self.root.resolve()), trusted.read_text())

    def test_init_without_hooks_trust_still_installs_and_says_so(self):
        self.python_repo()
        saved = getattr(hooks, "trust", None)
        if saved is not None:
            del hooks.trust
        try:
            result = cli.init(self.root.resolve(), "python", update=False, dry_run=False)
        finally:
            if saved is not None:
                hooks.trust = saved
        self.assertTrue(any("trust" in s for s in result["next_steps"]), result["next_steps"])
        self.assertTrue((self.root / "scripts/check").is_file())


# ── B12 / B15: verify ────────────────────────────────────────────────────────

class VerifyRun(Scratch):
    def proof(self, result, text):
        return next(p for p in result["proofs"] if text in p["proof"])

    def gate(self, body):
        self.python_repo()
        self.assertEqual(self.gf("init", str(self.root)).returncode, 0)
        self.write("scripts/check", body).chmod(0o755)
        self.commit("install slopbrake")

    def test_inherited_pr_and_base_variables_do_not_reach_the_proofs(self):
        self.gate('#!/bin/sh\necho "GH=${GITHUB_BASE_REF-unset} EV=${GITHUB_EVENT_PATH-unset} '
                  'BODY=${PR_BODY_FILE-unset} BASE=$SLOPBRAKE_BASE"\n')
        head = self.git("rev-parse", "HEAD")
        result = self.gf("verify", str(self.root), "--json", GITHUB_BASE_REF="main", GITHUB_EVENT_PATH="/x.json",
                         PR_BODY_FILE="/x.md", SLOPBRAKE_BASE="bogus")
        l1 = self.proof(json.loads(result.stdout), "clean tree passes")
        self.assertIn(f"GH=unset EV=unset BODY=unset BASE={head}", l1["output"])

    def test_a_noop_gate_fails_the_c8_proof(self):
        self.gate("#!/bin/sh\nexit 0\n")
        result = json.loads(self.gf("verify", str(self.root), "--json").stdout)
        c8 = self.proof(result, "a one-way change on the default branch blocks the commit")
        self.assertEqual(c8["rule"], "G2")
        self.assertFalse(c8["ok"])
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_the_real_gate_holds_the_c8_proof_without_touching_the_repo_refs(self):
        self.installed()
        before = sh(["git", "for-each-ref"], self.root).stdout
        result = json.loads(self.gf("verify", str(self.root), "--json").stdout)
        c8 = self.proof(result, "a one-way change on the default branch blocks the commit")
        self.assertTrue(c8["ok"], c8)
        self.assertEqual(sh(["git", "for-each-ref"], self.root).stdout, before)
        self.assertEqual(sh(["git", "status", "--porcelain"], self.root).stdout, "")
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_sigterm_removes_the_worktree_and_scratch_dir(self):
        self.gate("#!/bin/sh\nsleep 30\n")
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
        env.update(self.env)
        proc = subprocess.Popen([sys.executable, "-m", "slopbrake", "verify", str(self.root)], cwd=self.root, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 30
            while len(self.git("worktree", "list").splitlines()) < 2:
                self.assertLess(time.monotonic(), deadline, "verify never created its worktree")
                time.sleep(0.1)
            time.sleep(0.3)  # let it reach the gate
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)
        self.assertEqual(list(self.tmpdir.iterdir()), [])


# ── B16: one default-branch rule ─────────────────────────────────────────────

class DefaultBranch(Scratch):
    def setUp(self):
        super().setUp()
        empty = Path(self.tmp.name) / "gitconfig"
        empty.write_text("")
        # The machine's own init.defaultBranch must not decide these fixtures.
        patch = unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"})
        patch.start()
        self.addCleanup(patch.stop)

    def fixtures(self):
        """name -> (repo path, expected default branch)."""
        base = Path(self.tmp.name)
        out = {}

        def repo(name, branch="main"):
            path = base / name
            path.mkdir()
            self.git_repo(path, branch)
            return path

        out["main and master"] = (repo("both"), "main")
        sh(["git", "branch", "master"], out["main and master"][0])
        out["master only"] = (repo("master", "master"), "master")
        trunk = repo("trunk", "trunk")
        sh(["git", "config", "init.defaultBranch", "trunk"], trunk)
        sh(["git", "branch", "feat"], trunk)
        sh(["git", "branch", "main"], trunk)
        out["init.defaultBranch beats main"] = (trunk, "trunk")
        out["no known branch"] = (repo("other", "work"), None)
        origin = repo("origin-src", "develop")
        clone = base / "clone"
        sh(["git", "clone", "-q", str(origin), str(clone)], base)
        sh(["git", "branch", "main"], clone)
        out["origin/HEAD wins"] = (clone, "develop")
        return out

    def test_cli_follows_the_rule(self):
        for name, (path, expected) in self.fixtures().items():
            with self.subTest(name):
                self.assertEqual(cli.default_branch(path), expected)

    def test_cli_and_the_kit_agree(self):
        if "def default_branch" not in (KIT_COMMON / "common.py").read_text():
            self.skipTest("common.default_branch lands with the doors worker (B16)")
        code = "import sys; sys.path.insert(0, sys.argv[1]); import common; print(common.default_branch())"
        for name, (path, _) in self.fixtures().items():
            with self.subTest(name):
                kit = sh([sys.executable, "-c", code, str(KIT_COMMON)], path).stdout.strip()
                self.assertEqual(str(cli.default_branch(path)), kit)

    def test_workflow_branch_falls_back_to_the_current_branch(self):
        self.git_repo(branch="work")
        self.write("pyproject.toml", "[project]\nname = 'x'\n")
        self.assertEqual(cli.substitutions(self.root, "python")["DEFAULT_BRANCH"], "work")

    def test_init_hint_names_the_rule_s_default_not_a_feature_branch(self):
        self.git_repo(branch="trunk")
        sh(["git", "config", "init.defaultBranch", "trunk"], self.root)
        self.write("pyproject.toml", "[project]\nname = 'x'\n")
        self.write("tests/test_x.py", "")
        self.commit("app")
        sh(["git", "switch", "-q", "-c", "main"], self.root)  # a branch named main is not the default here
        steps = self.init_json()["next_steps"]
        self.assertFalse(any("one-way doors on main" in s for s in steps), steps)


if __name__ == "__main__":
    unittest.main()
