"""The TypeScript kit templates (scripts/check, CI, dependency-cruiser, Stryker), rendered the way
init renders them and run in throwaway npm projects whose node_modules/.bin tools are fakes."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
KIT = HOME / "slopbrake/kit"
TS = KIT / "typescript"
GUARDS = KIT / "common/scripts/slopbrake"
GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]

SUBS = {
    "DEPCRUISE_PATHS": "src tests",
    "TEST_GLOBS": "'tests/**/*.test.ts' 'src/**/*.test.ts'",
    "MUTATE_INCLUDE": "--include 'src/**/*.ts' --include 'src/**/*.tsx'",
}
# Runs one stage and exits with its raw status, so tests see the stage's own exit code.
STUB_RUNNER = 'run_stages() { "stage_${1//-/_}"; }\n'
# Records its argv one per line in $ARGV_DIR/<name>, prints $FAKE_OUT, then exits $FAKE_EXIT.
FAKE_TOOL = ('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$ARGV_DIR/$(basename "$0")"\n'
             '[ -z "${FAKE_OUT:-}" ] || echo "$FAKE_OUT"\nexit "${FAKE_EXIT:-0}"\n')
# A package manager that only logs: any `exec`/`install`/`add`/`dlx` in the log is a failure.
FAKE_PM = '#!/usr/bin/env bash\necho "$(basename "$0") $*" >> "$ARGV_DIR/pm.log"\n'


def render(text, pm):
    for key, value in {"PM": pm, **SUBS}.items():
        text = text.replace(f"@{key}@", value)
    return text


class Project(unittest.TestCase):
    """A throwaway TS repo with the kit's scripts/check rendered for `pm`."""
    pm = "npm"
    runner = STUB_RUNNER

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.argv = Path(tmp.name) / "argv"
        self.bin = Path(tmp.name) / "bin"
        self.argv.mkdir()
        self.bin.mkdir()
        for pm in ("npm", "pnpm", "yarn", "bun", "npx"):
            self.exe(self.bin / pm, FAKE_PM)
        shutil.copytree(GUARDS, self.root / "scripts/slopbrake", ignore=shutil.ignore_patterns("__pycache__"))
        if self.runner is not None:
            self.write("scripts/slopbrake/run-stages.sh", self.runner)
        self.exe(self.root / "scripts/check", render((TS / "scripts/check").read_text(), self.pm))
        self.write("package.json", '{"name": "t", "scripts": {"test": "vitest run"}}\n')
        self.write("src/a.ts", "export const a = 1;\n")
        self.write("tests/a.test.ts", "import { a } from '../src/a.js';\n")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git(*GIT_ID, "commit", "-q", "--no-verify", "-m", "init")

    def exe(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def tools(self, *names):
        for name in names:
            self.exe(self.root / "node_modules/.bin" / name, FAKE_TOOL)
        if "depcruise" in names:
            self.write("node_modules/typescript/package.json", '{"name": "typescript"}\n')

    def check(self, *stages, **env):
        full = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "ARGV_DIR": str(self.argv), **env}
        result = subprocess.run([str(self.root / "scripts/check"), *stages], cwd=self.root, env=full,
                                capture_output=True, text=True, timeout=60, check=False)
        result.out = result.stdout + result.stderr
        return result

    def argv_of(self, tool):
        path = self.argv / tool
        self.assertTrue(path.is_file(), f"{tool} was never run")
        return path.read_text().splitlines()

    def pm_log(self):
        path = self.argv / "pm.log"
        return path.read_text() if path.is_file() else ""

    def assert_nothing_installed(self):
        self.assertNotRegex(self.pm_log(), r"(?m)\b(exec|install|add|dlx|x)\b|^npx")


class ToolFlags(Project):
    def test_eslint_gets_every_flag_and_glob(self):
        self.tools("eslint")
        result = self.check("test-quality")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertEqual(self.argv_of("eslint"), ["--max-warnings", "0", "--no-error-on-unmatched-pattern",
                                                  "tests/**/*.test.ts", "src/**/*.test.ts"])
        self.assert_nothing_installed()

    def test_depcruise_keeps_its_config_flag(self):
        self.tools("depcruise")
        result = self.check("boundaries")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertEqual(self.argv_of("depcruise"), ["src", "tests", "--config", ".dependency-cruiser.cjs"])
        self.assert_nothing_installed()

    def test_tool_failure_fails_the_stage(self):
        self.tools("eslint")
        self.assertEqual(self.check("test-quality", FAKE_EXIT="1").returncode, 1)

    def test_stryker_gets_mutate_ranges_of_changed_source_only(self):
        self.tools("stryker")
        self.write("src/a.ts", "export const a = 1;\nexport const b = 2;\nexport const c = 3;\n")
        self.write("src/view.tsx", "export const v = 1;\n")
        for rel in ("src/a.test.ts", "src/b.spec.ts", "src/types.d.ts", "src/__tests__/helper.ts",
                    "src/deep/c.test.tsx"):
            self.write(rel, "export const x = 1;\n")
        result = self.check("mutation", SLOPBRAKE_BASE="HEAD")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertEqual(self.argv_of("stryker"),
                         ["run", "stryker.config.json", "--mutate", "src/a.ts:2-3,src/view.tsx:1-1"])
        self.assert_nothing_installed()


class PnpmFlags(ToolFlags):
    pm = "pnpm"


class MissingTools(Project):
    def assert_missing(self, stage, tool, package, pm="npm"):
        result = self.check(stage, SLOPBRAKE_BASE="HEAD")
        self.assertEqual(result.returncode, 1, result.out)
        self.assertIn(f"{tool} is not installed: {pm} add -D {package}", result.out)
        self.assert_nothing_installed()

    def test_missing_depcruise_names_the_real_package_and_installs_nothing(self):
        self.assert_missing("boundaries", "depcruise", "dependency-cruiser")
        self.assertNotIn("depcruise", self.pm_log())

    def test_missing_eslint(self):
        self.assert_missing("test-quality", "eslint", "eslint")

    def test_missing_stryker(self):
        self.write("src/a.ts", "export const a = 2;\n")
        self.assert_missing("mutation", "stryker", "@stryker-mutator/core")

    def test_boundaries_without_typescript_fails_instead_of_cruising_nothing(self):
        self.tools("depcruise")
        shutil.rmtree(self.root / "node_modules/typescript")
        self.assert_missing("boundaries", "typescript", "typescript")


    def test_yarn_pnp_says_why_the_tool_is_missing(self):
        self.write(".pnp.cjs", "")
        result = self.check("test-quality")
        self.assertEqual(result.returncode, 1, result.out)
        self.assertIn("eslint is not installed: npm add -D eslint", result.out)
        self.assertIn("nodeLinker: node-modules", result.out)


class BoundariesCruisedNothing(Project):
    """dependency-cruiser exits 0 having read no .ts file when it cannot load the installed typescript."""
    WARNING = ("  warn missing-typescript-transpiler: no compatible typescript transpiler found\n"
               "✔ no dependency violations found (0 modules, 0 dependencies cruised)")

    def setUp(self):
        super().setUp()
        self.tools("depcruise")

    def test_unsupported_typescript_fails_the_stage(self):
        result = self.check("boundaries", FAKE_OUT=self.WARNING)
        self.assertEqual(result.returncode, 1, result.out)
        self.assertIn("missing-typescript-transpiler", result.out)
        self.assertIn("dependency-cruiser cannot load the installed typescript: npm add -D typescript", result.out)
        self.assert_nothing_installed()

    def test_remedy_names_the_typescript_range_dependency_cruiser_supports(self):
        self.write("node_modules/dependency-cruiser/src/meta.cjs", """\
            module.exports = {
            \tsupportedTranspilers: {
            \t\tswc: ">=1.0.0 <2.0.0",
            \t\ttypescript: ">=2.0.0 <7.0.0",
            \t},
            };
            """)
        result = self.check("boundaries", FAKE_OUT=self.WARNING)
        self.assertIn("npm add -D 'typescript@>=2.0.0 <7.0.0'", result.out)
        shutil.rmtree(self.root / "node_modules/typescript")
        self.assertIn("typescript is not installed: npm add -D 'typescript@>=2.0.0 <7.0.0'",
                      self.check("boundaries").out)

    def test_zero_modules_cruised_fails_the_stage(self):
        result = self.check("boundaries", FAKE_OUT="✔ no dependency violations found (0 modules, 0 dependencies cruised)")
        self.assertEqual(result.returncode, 1, result.out)
        self.assertIn("cruised 0 modules", result.out)

    def test_modules_cruised_passes(self):
        result = self.check("boundaries", FAKE_OUT="✔ no dependency violations found (10 modules, 9 dependencies cruised)")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("10 modules", result.out)


class PnpmMissingTools(Project):
    pm = "pnpm"

    def test_message_uses_the_repo_package_manager(self):
        result = self.check("boundaries")
        self.assertIn("depcruise is not installed: pnpm add -D dependency-cruiser", result.out)
        self.assert_nothing_installed()


class Skips(Project):
    def assert_skip(self, result, stage):
        self.assertEqual(result.returncode, 78, result.out)
        self.assertRegex(result.out, rf"(?m)^{stage}: skipped: \S")

    def test_types_skips_without_a_typecheck_script(self):
        self.assert_skip(self.check("types"), "types")
        self.assertEqual(self.pm_log(), "")

    def test_types_runs_the_typecheck_script(self):
        self.write("package.json", '{"scripts": {"typecheck": "tsc --noEmit"}}\n')
        self.assertEqual(self.check("types").returncode, 0)
        self.assertEqual(self.pm_log(), "npm run -s typecheck\n")

    def test_smoke_skips_without_an_entry_point(self):
        self.assert_skip(self.check("smoke"), "smoke")

    def test_smoke_runs_the_package_json_smoke_script(self):
        self.write("package.json", '{"scripts": {"smoke": "node dist/cli.js --help"}}\n')
        self.assertEqual(self.check("smoke").returncode, 0)
        self.assertEqual(self.pm_log(), "npm run -s smoke\n")

    def test_smoke_script_file_wins_and_its_failure_counts(self):
        self.write("package.json", '{"scripts": {"smoke": "true"}}\n')
        self.write("scripts/smoke.sh", "echo smoke-sh-ran; exit 3\n")
        result = self.check("smoke")
        self.assertEqual(result.returncode, 3)
        self.assertIn("smoke-sh-ran", result.out)
        self.assertEqual(self.pm_log(), "")

    def test_broken_package_json_fails_instead_of_skipping(self):
        self.write("package.json", '{"scripts": {"typecheck": ')
        for stage in ("types", "smoke"):
            result = self.check(stage)
            self.assertEqual(result.returncode, 1, result.out)
            self.assertIn("package.json", result.out)
            self.assertNotIn("skipped", result.out)

    def test_mutation_skips_when_no_source_changed(self):
        self.tools("stryker")
        self.write("tests/a.test.ts", "// only a test changed\n")
        self.assert_skip(self.check("mutation", SLOPBRAKE_BASE="HEAD"), "mutation")
        self.assertFalse((self.argv / "stryker").exists())


@unittest.skipUnless("78" in (GUARDS / "run-stages.sh").read_text(), "run-stages.sh predates the skip code (C1)")
class SkipSummary(Project):
    runner = None  # the kit's real run-stages.sh

    def test_skipped_stages_report_skip_and_do_not_fail_the_gate(self):
        result = self.check("types", "smoke")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertRegex(result.out, r"(?m)^skip +types\b")
        self.assertRegex(result.out, r"(?m)^skip +smoke\b")


class CiTemplate(unittest.TestCase):
    text = (TS / ".github/workflows/check.yml").read_text()

    def test_package_manager_steps_are_placeholders(self):
        self.assertNotIn("pnpm", self.text)
        self.assertIn("\n@CI_SETUP@\n", self.text)
        self.assertRegex(self.text, r"\n      - run: @CI_INSTALL@\n")

    def test_setup_goes_right_after_checkout(self):
        setup = "      - uses: actions/setup-node@v4\n        with:\n          node-version: 22\n          cache: npm\n"
        out = self.text.replace("@CI_SETUP@\n", setup).replace("@CI_INSTALL@", "npm ci")
        out = out.replace("@DEFAULT_BRANCH@", "main")
        self.assertNotRegex(out, r"@[A-Z_]+@")
        steps = re.findall(r"(?m)^      - (.*)$", out)
        self.assertEqual(steps, ["uses: actions/checkout@v4", "uses: actions/setup-node@v4",
                                 "uses: actions/setup-python@v5", "run: npm ci", "run: scripts/check"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DepcruiseConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "dc.cjs"
        path.write_text((TS / "dependency-cruiser.cjs.tmpl").read_text().replace("@PACKAGES_ROOT@", "src"))
        out = subprocess.run(["node", "-e", f"console.log(JSON.stringify(require({str(path)!r})))"],
                             capture_output=True, text=True, check=True).stdout
        self.config = json.loads(out)

    def violations(self, source, target):
        """Rules that forbid source -> target, with dependency-cruiser's $1 group substitution."""
        hits = []
        for rule in self.config["forbidden"]:
            frm, to = rule["from"], rule["to"]
            if "circular" in to:
                continue
            m = re.search(frm["path"], source) if "path" in frm else re.match("", source)
            if not m or ("pathNot" in frm and re.search(frm["pathNot"], source)):
                continue
            group = m.group(1) if m.re.groups else ""
            path, path_not = (to.get(k, "(?!)").replace("$1", group) for k in ("path", "pathNot"))
            if re.search(path, target) and not re.search(path_not, target):
                hits.append(rule["name"])
        return hits

    def test_type_only_imports_are_cruised(self):
        self.assertIs(self.config["options"]["tsPreCompilationDeps"], True)

    def test_colocated_tests_go_through_their_own_entry_points(self):
        for test in ("src/billing/ledger.test.ts", "src/billing/ledger.spec.tsx", "src/billing/lib/impl.test.ts"):
            self.assertTrue(self.violations(test, "src/billing/lib/impl.ts"), test)
        self.assertEqual(self.violations("src/billing/ledger.test.ts", "src/billing/index.ts"), [])
        self.assertEqual(self.violations("src/billing/lib/impl.ts", "src/billing/lib/util.ts"), [])
        self.assertTrue(self.violations("src/ui/view.test.ts", "src/billing/lib/impl.ts"))


class StrykerConfig(unittest.TestCase):
    config = json.loads((TS / "stryker.config.json").read_text())

    def test_floor_and_modest_concurrency(self):
        self.assertGreaterEqual(self.config["thresholds"]["break"], 60)
        self.assertEqual(self.config["concurrency"], 2)
        self.assertNotIn("mutate", self.config)  # scripts/check passes --mutate with the changed ranges


if __name__ == "__main__":
    unittest.main()
