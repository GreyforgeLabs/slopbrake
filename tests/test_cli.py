"""Installer behaviour (init, status, verify) through the command line and cli's public functions."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))

from slopbrake import __version__, cli

GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]
NOOP_CHECK = '#!/bin/sh\necho "NO_RECORD=$SLOPBRAKE_NO_RECORD"\n'


def sh(cmd, cwd, env=None, stdin=None):
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.tmpdir = Path(self.tmp.name) / "tmp"  # TMPDIR for the cli, to catch leaked scratch dirs
        self.tmpdir.mkdir()
        (Path(self.tmp.name) / "home").mkdir()  # HOME for the cli: never the user's real settings

    def write(self, rel, text, base=None):
        path = (base or self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def git_repo(self, path=None):
        path = path or self.root
        sh(["git", "init", "-q", "-b", "main"], path)
        self.write("README.md", "# demo\n", base=path)
        self.commit("init", path)

    def commit(self, message, path=None):
        sh(["git", "add", "-A"], path or self.root)
        result = sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", message], path or self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def gf(self, *args, cwd=None, path_prefix=None, **env):
        # init records the repo in the user-level trust list (B11 b) and status reads ~/.claude/settings.json:
        # keep both away from the real ~/.config and HOME (the uv cache stays real: no ruff re-download).
        full = dict(os.environ, PYTHONPATH=str(HOME), TMPDIR=str(self.tmpdir),
                    XDG_CONFIG_HOME=str(Path(self.tmp.name) / "config"), HOME=str(Path(self.tmp.name) / "home"),
                    UV_CACHE_DIR=os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv")), **env)
        if path_prefix:
            full["PATH"] = f"{path_prefix}{os.pathsep}{full['PATH']}"
        return sh([sys.executable, "-m", "slopbrake", *args], cwd or self.root, env=full)

    def python_repo(self, path=None):
        path = path or self.root
        self.git_repo(path)
        self.write("pyproject.toml", "[project]\nname = 'shop'\n", base=path)
        self.write("shop.py", "def one():\n    return 1\n", base=path)
        self.write("tests/test_shop.py", "import unittest\nfrom shop import one\n\n\nclass T(unittest.TestCase):\n"
                                         "    def test_one(self):\n        self.assertEqual(one(), 1)\n", base=path)
        self.commit("app", path)

    def fake_bin(self, name, body):
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        tool = bindir / name
        tool.write_text(f"#!/bin/sh\n{body}\n")
        tool.chmod(0o755)
        return bindir

    def assert_one_line_error(self, result):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        self.assertTrue(result.stderr.startswith("slopbrake: "), result.stderr)

    def assert_nothing_leaked(self, repo=None):
        self.assertEqual(list(self.tmpdir.iterdir()), [])
        worktrees = sh(["git", "worktree", "list"], repo or self.root).stdout.strip().splitlines()
        self.assertLessEqual(len(worktrees), 1, worktrees)


class Init(Scratch):
    def test_dry_run_on_a_fresh_python_repo_prints_the_plan_and_writes_nothing(self):
        self.python_repo()
        result = self.gf("init", str(self.root), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("create  scripts/check", result.stdout)
        self.assertIn("choose a type checker", result.stdout)
        self.assertFalse((self.root / "scripts/check").exists())
        self.assertEqual(sh(["git", "config", "--get", "core.hooksPath"], self.root).stdout, "")
        self.assertEqual(sh(["git", "for-each-ref", "refs/slopbrake"], self.root).stdout, "")

    def test_environment_errors_exit_2_with_one_line(self):
        self.write("pyproject.toml", "[project]\nname = 'x'\n")  # not a git repo
        self.assert_one_line_error(self.gf("init", str(self.root)))
        self.assert_one_line_error(self.gf("init", str(self.root / "nope")))
        self.assert_one_line_error(self.gf("status", str(self.root)))
        result = self.gf("init", str(self.root), "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not inside a git repository", json.loads(result.stdout)["error"])
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        sh(["git", "init", "-q"], empty)
        self.assert_one_line_error(self.gf("init", str(empty)))  # no supported stack

    def test_unparsable_settings_fail_before_anything_is_written(self):
        self.python_repo()
        self.write(".claude/settings.json", '{\n  // my comment\n  "model": "x"\n}\n')
        self.assert_one_line_error(self.gf("init", str(self.root)))
        self.assertFalse((self.root / ".claude/hooks").exists())
        self.assertFalse((self.root / "scripts/check").exists())
        self.write(".claude/settings.json", json.dumps({"hooks": {"Stop": {"command": "x"}}}))
        self.assert_one_line_error(self.gf("init", str(self.root)))
        self.assertFalse((self.root / ".claude/hooks").exists())

    def test_settings_merge_tolerates_null_hooks_and_respects_matchers(self):
        self.python_repo()
        guard = '"$CLAUDE_PROJECT_DIR"/.claude/hooks/block-dangerous-git.sh'
        self.write(".claude/settings.json", json.dumps({"hooks": None}))
        self.assertEqual(self.gf("init", str(self.root)).returncode, 0)
        hooks = json.loads((self.root / ".claude/settings.json").read_text())["hooks"]
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash")
        merged = cli.merge_settings({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"command": guard}]}]}},
                                    json.loads((cli.KIT / "common/.claude/settings.json").read_text()))
        self.assertEqual([e["matcher"] for e in merged["hooks"]["PreToolUse"]], ["Edit", "Bash"])

    def next_steps(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return [line for line in result.stdout.splitlines() if line.strip().startswith("next")]

    def test_init_starts_the_branch_last_green_ratchet_and_says_to_commit_before_verify(self):
        self.python_repo()
        head = sh(["git", "rev-parse", "HEAD"], self.root).stdout.strip()
        steps = self.next_steps(self.gf("init", str(self.root)))
        refs = sh(["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/slopbrake"], self.root).stdout
        self.assertEqual(refs.split(), ["refs/slopbrake/last-green/main", head])
        # The per-branch namespace stays open for the other branches (A1).
        self.assertEqual(sh(["git", "update-ref", "refs/slopbrake/last-green/feature", "HEAD"], self.root).returncode, 0)
        commit = next(i for i, s in enumerate(steps) if "git commit" in s)
        verify = next(i for i, s in enumerate(steps) if "slopbrake verify" in s)
        self.assertLess(commit, verify)

    def test_detached_head_gets_no_ratchet(self):
        self.python_repo()
        sh(["git", "checkout", "-q", "--detach"], self.root)
        self.assertEqual(self.gf("init", str(self.root)).returncode, 0)
        self.assertEqual(sh(["git", "for-each-ref", "refs/slopbrake"], self.root).stdout, "")

    def test_an_old_flat_ratchet_ref_is_replaced_by_the_branch_ref(self):
        self.python_repo()
        sh(["git", "update-ref", "refs/slopbrake/last-green", "HEAD"], self.root)
        self.assertEqual(self.gf("init", str(self.root)).returncode, 0)
        refs = sh(["git", "for-each-ref", "--format=%(refname)", "refs/slopbrake"], self.root).stdout.split()
        self.assertEqual(refs, ["refs/slopbrake/last-green/main"])

    def test_commit_step_avoids_the_one_way_block_on_the_default_branch(self):
        self.python_repo()
        self.write("notes.txt", "unrelated work in progress\n")
        step = next(s for s in self.next_steps(self.gf("init", str(self.root))) if "git commit" in s)
        self.assertIn("git switch -c", step)  # the pr stage blocks the kit (one-way) on main
        self.assertIn("--no-verify", step)  # ...unless a human overrides it
        self.assertNotIn("-A", step)
        self.assertNotIn("notes.txt", step)
        self.assertIn("scripts/check", step)
        self.commit("kit")
        sh(["git", "switch", "-q", "-c", "work"], self.root)
        steps = self.next_steps(self.gf("init", str(self.root), "--update"))
        self.assertFalse(any("git commit" in s for s in steps), steps)
        (self.root / "scripts/slopbrake/common.py").write_text("# an older kit\n")
        self.commit("older kit")
        step = next(s for s in self.next_steps(self.gf("init", str(self.root), "--update")) if "git commit" in s)
        self.assertNotIn("git switch", step)
        self.assertIn("scripts/slopbrake", step)

    def test_monorepo_subdir_never_takes_over_the_parent_hooks(self):
        # B8 (round 2): subdirectory installs are refused outright, before anything is written.
        self.git_repo()
        app = self.root / "packages/app"
        self.write("packages/app/pyproject.toml", "[project]\nname = 'app'\n")
        self.assert_one_line_error(self.gf("init", str(app)))
        self.assertEqual(sh(["git", "config", "--get", "core.hooksPath"], self.root).stdout, "")
        self.assertFalse((app / ".claude").exists())

    def test_linked_worktree_sees_the_shared_hooks(self):
        self.python_repo()
        hook = self.write(".git/hooks/pre-commit", "#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        linked = Path(self.tmp.name) / "linked"
        sh(["git", "worktree", "add", "-q", str(linked)], self.root)
        result = json.loads(self.gf("init", str(linked), "--json").stdout)
        self.assertIn("left alone", result["hooks"])
        self.assertEqual(sh(["git", "config", "--get", "core.hooksPath"], self.root).stdout, "")

    def test_update_with_auto_stack_keeps_the_recorded_stack(self):
        self.python_repo()
        self.write("package.json", '{"name": "web"}\n')
        self.write("tsconfig.json", "{}\n")
        self.commit("web bits")
        self.assertEqual(self.gf("init", str(self.root), "--stack", "python").returncode, 0)
        result = json.loads(self.gf("init", str(self.root), "--update", "--json").stdout)
        self.assertEqual(result["stack"], "python")
        self.assertFalse((self.root / "eslint-rules").exists())
        self.assertEqual(json.loads((self.root / ".claude/slopbrake.json").read_text())["stack"], "python")


class PythonDefaults(Scratch):
    def test_pytest_is_detected_from_any_config_or_declaration(self):
        layouts = {
            "pyproject section": {"pyproject.toml": "[tool.pytest.ini_options]\n"},
            "pytest.ini": {"pytest.ini": "[pytest]\n"},
            "tox.ini": {"tox.ini": "[pytest]\naddopts = -q\n"},
            "setup.cfg": {"setup.cfg": "[tool:pytest]\n"},
            "nested conftest": {"tests/conftest.py": ""},
            "dependency group": {"pyproject.toml": "[project]\nname='x'\n[dependency-groups]\ndev = ['pytest>=8']\n"},
            "optional deps": {"pyproject.toml": "[project]\nname='x'\n[project.optional-dependencies]\ntest = ['pytest']\n"},
        }
        for name, files in layouts.items():
            with self.subTest(layout=name), tempfile.TemporaryDirectory() as tmp:
                for rel, text in files.items():
                    self.write(rel, text, base=Path(tmp))
                self.assertEqual(cli.python_test_args(Path(tmp)), "-m pytest -q")
        with tempfile.TemporaryDirectory() as tmp:
            self.write("pyproject.toml", "[project]\nname='x'\ndependencies = ['pytest-ish-lib']\n", base=Path(tmp))
            self.assertEqual(cli.python_test_args(Path(tmp)), "-m unittest discover -s tests")
    # B6 (round 2): the interpreter is chosen by scripts/check at run time; see tests/test_r2_cli.py.

    def test_ci_install_matches_the_test_command(self):
        self.write("pyproject.toml", "[project]\nname='x'\n[project.optional-dependencies]\ndev = ['pytest']\n"
                                     "docs = ['mkdocs']\n")
        subs = cli.substitutions(self.root, "python")
        self.assertEqual(subs["CI_INSTALL"], "python3 -m pip install -e '.[dev]' pytest")
        (self.root / ".venv").mkdir()
        self.assertEqual(cli.substitutions(self.root, "python")["CI_INSTALL"],
                         "python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]' pytest")
        self.write("uv.lock", "")
        self.assertEqual(cli.substitutions(self.root, "python")["CI_INSTALL"], "uv sync --frozen --all-extras --all-groups")
        with tempfile.TemporaryDirectory() as tmp:
            self.write("pyproject.toml", "[project]\nname='x'\n", base=Path(tmp))
            self.assertEqual(cli.substitutions(Path(tmp), "python")["CI_INSTALL"], "python3 -m pip install -e .")

    def test_unconfigured_types_stage_skips_with_code_78(self):
        types_cmd = cli.substitutions(self.root, "python")["TYPES_CMD"]
        result = sh(["bash", "-c", f"stage_types() {{ {types_cmd}; }}; stage_types"], self.root)
        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stdout.strip(), "types: skipped: no type checker configured (edit scripts/check)")


class TypescriptTemplates(Scratch):
    def subs(self, files):
        for rel, text in files.items():
            self.write(rel, text)
        return cli.substitutions(self.root, "typescript")

    def test_ci_setup_and_install_follow_the_package_manager(self):
        cases = {
            "pnpm-lock.yaml": ("pnpm/action-setup@v4", "cache: pnpm", "pnpm install --frozen-lockfile"),
            "package-lock.json": ("actions/setup-node@v4", "cache: npm", "npm ci"),
            "yarn.lock": ("corepack enable", "cache: yarn", "yarn install --frozen-lockfile"),
            "bun.lockb": ("oven-sh/setup-bun@v2", "", "bun install --frozen-lockfile"),
        }
        for lock, (setup, cache, install) in cases.items():
            with self.subTest(lock=lock):
                for old in self.root.iterdir():
                    old.unlink()
                subs = self.subs({"package.json": '{"name": "t"}', lock: ""})
                self.assertIn(setup, subs["CI_SETUP"])
                self.assertIn(cache, subs["CI_SETUP"])
                self.assertEqual(subs["CI_INSTALL"], install)
                self.assertTrue(all(line.startswith("      ") for line in subs["CI_SETUP"].splitlines()))
                self.assertTrue(subs["CI_SETUP"].lstrip().startswith("- "))
                self.assertNotIn("pnpm", subs["CI_SETUP"] + subs["CI_INSTALL"] if lock != "pnpm-lock.yaml" else "")

    def test_node_version_comes_from_the_repo_when_it_declares_one(self):
        self.assertIn("node-version: 22", self.subs({"package.json": '{"name": "t"}'})["CI_SETUP"])
        self.assertIn("node-version-file: package.json",
                      self.subs({"package.json": '{"engines": {"node": ">=20"}}'})["CI_SETUP"])
        self.write("package.json", '{"name": "t"}')
        self.assertIn("node-version-file: .nvmrc", self.subs({".nvmrc": "20\n"})["CI_SETUP"])

    def test_mutate_include_and_test_globs_cover_tsx_and_spec_files(self):
        (self.root / "src").mkdir()
        subs = self.subs({"package.json": "{}"})
        self.assertEqual(subs["MUTATE_INCLUDE"], "--include 'src/**/*.ts' --include 'src/**/*.tsx'")
        for glob in ("'tests/**/*.test.ts'", "'tests/**/*.spec.tsx'", "'src/**/*.test.tsx'", "'src/**/*.spec.ts'"):
            self.assertIn(glob, subs["TEST_GLOBS"])
        (self.root / "src").rmdir()
        (self.root / "lib").mkdir()
        self.assertIn("--include 'lib/**/*.ts' --include 'lib/**/*.tsx'", self.subs({})["MUTATE_INCLUDE"])


class Status(Scratch):
    def installed(self):
        self.python_repo()
        self.assertEqual(self.gf("init", str(self.root)).returncode, 0)
        self.commit("install slopbrake")

    def gaps(self, **env):
        result = self.gf("status", str(self.root), "--json", **env)
        return json.loads(result.stdout)[0]["gaps"]

    def test_fresh_install_only_lacks_a_type_checker(self):
        self.installed()
        self.assertEqual(self.gaps(), ["types stage is not configured: choose a type checker in scripts/check"])

    def test_unwired_or_disabled_hooks_are_gaps(self):
        self.installed()
        self.write(".claude/settings.json", "{}\n")
        self.write(".claude/settings.local.json", '{"disableAllHooks": true}\n')
        (self.root / ".claude/hooks/require-green.sh").chmod(0o644)
        gaps = "\n".join(self.gaps())
        self.assertIn("block-dangerous-git.sh", gaps)
        self.assertIn("require-green.sh on Stop", gaps)
        self.assertIn("settings.local.json sets disableAllHooks", gaps)
        self.assertIn(".claude/hooks/require-green.sh is not executable", gaps)

    def test_git_hooks_path_must_exist_with_both_hooks(self):
        self.installed()
        sh(["git", "rm", "-rq", ".githooks"], self.root)
        self.assertIn("core.hooksPath=.githooks does not exist", "\n".join(self.gaps()))

    def test_missing_python3_is_a_gap(self):
        self.installed()
        bindir = Path(self.tmp.name) / "gitonly"
        bindir.mkdir()
        (bindir / "git").symlink_to(sh(["sh", "-c", "command -v git"], self.root).stdout.strip())
        result = sh([sys.executable, "-m", "slopbrake", "status", str(self.root), "--json"], self.root,
                    env=dict(os.environ, PYTHONPATH=str(HOME), PATH=str(bindir)))
        self.assertIn("python3 is not on PATH", "\n".join(json.loads(result.stdout)[0]["gaps"]))

    def test_stale_kit_is_a_gap(self):
        self.installed()
        common = self.root / "scripts/slopbrake/common.py"
        common.write_text(common.read_text() + "# old\n")
        meta = self.root / ".claude/slopbrake.json"
        meta.write_text(json.dumps({"stack": "python", "kit": "slopbrake", "kit_version": "0.1.0+98ea342"}))
        gaps = self.gaps()
        self.assertIn("kit is stale: 1 managed file(s) differ; run slopbrake init --update", gaps)
        self.assertIn(f"kit is stale: installed 0.1.0+98ea342, running slopbrake {__version__}; "
                      "run slopbrake init --update", gaps)

    def test_typescript_repo_without_the_eslint_rule_wired_is_a_gap(self):
        self.git_repo()
        self.write("package.json", '{"name": "t"}')
        self.write("tsconfig.json", "{}")
        self.write("eslint.config.mjs", "export default [];\n")
        self.gf("init", str(self.root))
        self.assertIn("eslint config does not load eslint-rules/slopbrake.mjs (T1 is not enforced)", self.gaps())
        self.write("eslint.config.mjs", "import slopbrake from './eslint-rules/slopbrake.mjs';\nexport default [];\n")
        self.assertNotIn("eslint config does not load eslint-rules/slopbrake.mjs (T1 is not enforced)", self.gaps())


class Verify(Scratch):
    def noop_repo(self, path=None):
        """Installed and committed, with a scripts/check that runs nothing."""
        path = path or self.root
        self.python_repo(path)
        self.assertEqual(self.gf("init", str(path)).returncode, 0)
        check = self.write("scripts/check", NOOP_CHECK, base=path)
        check.chmod(0o755)
        self.commit("install slopbrake", path)

    def verify(self, repo=None, **kwargs):
        result = self.gf("verify", str(repo or self.root), "--json", **kwargs)
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def proof(result, text):
        return next(p for p in result["proofs"] if text in p["proof"])

    def test_a_noop_gate_fails_every_expect_pass_proof_that_needs_evidence(self):
        self.noop_repo()
        result = self.verify()
        self.assertFalse(result["ok"])
        l1 = self.proof(result, "clean tree passes")
        self.assertFalse(l1["ok"])
        self.assertIn("NO_RECORD=1", l1["output"])  # verify never records last-green
        self.assertTrue(any("lint types boundaries tests smoke test-quality mutation pr" in line for line in l1["output"]))
        self.assertFalse(self.proof(result, "boundaries pass again")["ok"])
        self.assertFalse(self.proof(result, "real assertions clear")["ok"])
        self.assertTrue(self.proof(result, "hooks are wired")["ok"])
        self.assert_nothing_leaked()

    def test_trimmed_stages_fail_the_l1_proof(self):
        self.python_repo()
        self.gf("init", str(self.root))
        check = self.root / "scripts/check"
        check.write_text(check.read_text().replace("STAGES=(lint types boundaries tests smoke test-quality mutation pr)",
                                                   "STAGES=(tests)"))
        self.commit("trim the gate")
        l1 = self.proof(self.verify(), "clean tree passes")
        self.assertFalse(l1["ok"])
        self.assertTrue(any("boundaries" in line and "missing" in line for line in l1["output"]), l1["output"])

    def test_unwired_hooks_fail_the_wiring_proof(self):
        self.noop_repo()
        self.write(".claude/settings.json", "{}\n")
        self.commit("unwire")
        self.write(".claude/settings.local.json", '{"disableAllHooks": true}\n')
        wiring = self.proof(self.verify(), "hooks are wired")
        self.assertEqual(wiring["rule"], "H5/H6")
        self.assertFalse(wiring["ok"])
        self.assertTrue(any("disableAllHooks" in line for line in wiring["output"]))

    def test_proofs_read_the_committed_head_not_the_working_tree(self):
        self.noop_repo()
        self.write("package.json", '{"name": "web"}\n')
        self.commit("package.json in a python repo")
        self.write("CLAUDE.md", "x\n" * 60)
        meta = self.root / ".claude/slopbrake.json"
        meta.write_text(meta.read_text().replace('"python"', '"typescript"'))
        marker = Path(self.tmp.name) / "npm-ran"
        bindir = self.fake_bin("npm", f"touch {marker}; exit 1")
        result = self.verify(path_prefix=str(bindir))
        self.assertEqual(result["stack"], "python")
        self.assertFalse(marker.exists())
        self.assertTrue(self.proof(result, "CLAUDE.md is at most")["ok"])

    def test_verify_refuses_a_monorepo_subdir(self):
        # B8 (round 2): verify refuses subdirectory installs like init and status.
        self.git_repo()
        app = self.root / "packages/app"
        self.write("packages/app/pyproject.toml", "[project]\nname = 'app'\n")
        self.commit("app")
        self.assert_one_line_error(self.gf("verify", str(app)))
        self.assert_nothing_leaked()

    def test_no_commits_is_an_environment_error_and_leaks_nothing(self):
        sh(["git", "init", "-q"], self.root)
        self.write("pyproject.toml", "[project]\nname = 'x'\n")
        self.assert_one_line_error(self.gf("verify", str(self.root)))
        self.assert_nothing_leaked()

    def test_missing_dir_is_an_environment_error(self):
        missing = str(self.root / "nope")
        self.assert_one_line_error(self.gf("verify", missing))
        result = self.gf("verify", missing, "--json")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("no such directory", json.loads(result.stdout)["error"])

    def test_head_without_a_kit_points_to_init_not_to_a_missing_option(self):
        self.git_repo()  # committed, but no stack and no kit
        result = self.gf("verify", str(self.root))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("HEAD has no slopbrake kit", result.stdout)
        self.assertNotIn("--stack", result.stdout + result.stderr)
        self.assertNotIn("slopbrake-verify-", result.stdout + result.stderr)
        self.assert_nothing_leaked()

    def test_failed_dependency_install_exits_2_with_json_and_leaks_nothing(self):
        self.git_repo()
        self.write("package.json", '{"name": "t"}')
        self.write("tsconfig.json", "{}")
        self.gf("init", str(self.root))
        self.commit("install slopbrake")
        result = self.gf("verify", str(self.root), "--json", path_prefix=str(self.fake_bin("npm", "exit 1")))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("failed in the verify worktree", json.loads(result.stdout)["error"])
        self.assert_nothing_leaked()

    def test_failed_install_error_names_the_cause_not_the_log_file(self):
        self.git_repo()
        self.write("package.json", '{"name": "t"}')
        self.write("tsconfig.json", "{}")
        self.gf("init", str(self.root))
        self.commit("install slopbrake")
        npm = ("echo 'npm error code EUSAGE' >&2; echo 'npm error' >&2; "
               "echo 'npm error The npm ci command can only install with an existing package-lock.json' >&2; "
               "echo 'npm error A complete log of this run can be found in: /x/debug.log' >&2; exit 1")
        result = self.gf("verify", str(self.root), "--json", path_prefix=str(self.fake_bin("npm", npm)))
        self.assertIn("existing package-lock.json", json.loads(result.stdout)["error"])

    def test_failed_install_error_keeps_a_wrapped_message_whole(self):
        self.git_repo()
        self.write("package.json", '{"name": "t"}')
        self.write("tsconfig.json", "{}")
        self.gf("init", str(self.root))
        self.commit("install slopbrake")
        npm = ("echo 'npm error code EUSAGE' >&2; echo 'npm error' >&2; "
               "echo 'npm error The npm ci command can only install with an existing package-lock.json or' >&2; "
               "echo 'npm error npm-shrinkwrap.json with lockfileVersion >= 1. Run an install with npm@5 or' >&2; "
               "echo 'npm error later to generate a package-lock.json file, then try again.' >&2; "
               "echo 'npm error' >&2; echo 'npm error Clean install a project' >&2; exit 1")
        error = json.loads(self.gf("verify", str(self.root), "--json",
                                   path_prefix=str(self.fake_bin("npm", npm))).stdout)["error"]
        self.assertIn("existing package-lock.json or npm-shrinkwrap.json", error)
        self.assertIn("then try again.", error)
        self.assertNotIn("Clean install", error)

    def test_failure_output_shows_the_failing_stage_summary(self):
        self.noop_repo()
        self.write("scripts/check", "#!/bin/sh\necho '── summary ──'\necho 'FAIL  lint (0s)'\n"
                                    "for i in 1 2 3 4 5 6 7; do echo noise $i; done\nexit 1\n").chmod(0o755)
        self.commit("red gate")
        l1 = self.proof(self.verify(), "clean tree passes")
        self.assertIn("FAIL  lint (0s)", l1["output"])


class VerifyProofHelpers(unittest.TestCase):
    def test_required_stages_come_from_the_kit_template(self):
        self.assertEqual(cli.kit_stages("python"),
                         ["lint", "types", "boundaries", "tests", "smoke", "test-quality", "mutation", "pr"])

    def test_summary_counts_pass_and_skip_but_not_fail(self):
        out = "── tests ──\nok\n── summary ──\npass  lint (1s)\nskip  mutation (nothing changed)\nFAIL  tests (0s)\n"
        self.assertEqual(cli.missing_stages(out, ["lint", "tests", "mutation", "pr"]), ["tests", "pr"])


class Version(unittest.TestCase):
    def test_the_version_agrees_everywhere(self):
        self.assertIn(f'version = "{__version__}"', (HOME / "pyproject.toml").read_text())
        self.assertIn(f"## {__version__} (", (HOME / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
