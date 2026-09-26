#!/usr/bin/env python3
"""slopbrake: put brakes on coding agents: install the checks into a repo and prove they bite.

  slopbrake init REPO [--stack auto|python|typescript] [--update] [--dry-run] [--json]
  slopbrake status REPO... [--json]
  slopbrake verify REPO [--json] [--keep]

init writes two kinds of files. Managed files (scripts/slopbrake, hooks, the reviewer
agent, vendored skills) belong to the kit: --update refreshes them. Seeded files
(CLAUDE.md, CODING_STANDARDS.md, door rules, scripts/check, retro log, CI) belong to the
repo once written: init never overwrites them. verify runs a proof for each rule
in a throwaway git worktree, so the repo's working tree is never touched.
Exit codes: 0 ok, 1 a check or proof failed, 2 usage or environment error.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

HOME = Path(__file__).resolve().parent  # the package: kit/ and vendor/ ship inside it
KIT = HOME / "kit"
VENDOR = HOME / "vendor" / "pocock-skills"

# name -> vendored path. Overlays in kit/overlays/<name>.md are appended to SKILL.md.
VENDORED_SKILLS = {
    "tdd": "engineering/tdd",
    "pr": "in-progress/pr",
    "retro": "in-progress/retro",
    "diagnosing-bugs": "engineering/diagnosing-bugs",
    "codebase-design": "engineering/codebase-design",
    "writing-for-agents": "productivity/writing-for-agents",
    "to-spec": "engineering/to-spec",
    "to-tickets": "engineering/to-tickets",
    "handoff": "productivity/handoff",
    "improve-codebase-architecture": "engineering/improve-codebase-architecture",
    "domain-modeling": "engineering/domain-modeling",
}
MANAGED_PREFIXES = ("scripts/slopbrake/", ".claude/hooks/", ".claude/agents/reviewer.md", ".claude/skills/",
                    ".claude/slopbrake.json", ".githooks/", "eslint-rules/slopbrake.mjs")
SPEC_FILES = ["CLAUDE.md", "CODING_STANDARDS.md", ".claude/agents/reviewer.md",
              ".claude/skills/implement-ticket/SKILL.md", ".claude/skills/tdd/SKILL.md", ".claude/skills/pr/SKILL.md",
              ".claude/skills/retro/SKILL.md", ".claude/settings.json", ".claude/door-rules.yml", "scripts/check",
              "docs/agents/retro-log.md", "docs/agents/issue-tracker.md"]
CLAUDE_MD_MAX_LINES = 40
GITIGNORE = {"python": ["__pycache__/", ".ruff_cache/", ".mypy_cache/"], "typescript": [".stryker-tmp/", "reports/"]}


@dataclass
class Action:
    path: str
    action: str  # create | update | unchanged | kept | merge
    note: str = ""


def run(cmd, cwd: Path, env=None, check=False) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, shell=isinstance(cmd, str), check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"{cmd} failed in {cwd}:\n{result.stdout}{result.stderr}")
    return result


def git(repo: Path, *args: str, check=True) -> str:
    return run(["git", *args], repo, check=check).stdout.strip()


def kit_version() -> str:
    """Package version, plus the commit when running from a git checkout of the kit."""
    from slopbrake import __version__
    sha = git(HOME, "rev-parse", "--short", "HEAD", check=False) if (HOME.parent / ".git").exists() else ""
    return f"{__version__}+{sha}" if sha else __version__


# ── detection ────────────────────────────────────────────────────────────────

def detect_stack(repo: Path) -> str:
    if (repo / "package.json").is_file() and (repo / "tsconfig.json").is_file():
        return "typescript"
    if (repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file():
        return "python"
    raise SystemExit(f"slopbrake: cannot detect a supported stack in {repo} (python, typescript); pass --stack")


def github_remote(repo: Path) -> bool:
    return "github.com" in git(repo, "remote", "-v", check=False)


def default_branch(repo: Path) -> str:
    for name in ("main", "master"):
        if run(["git", "rev-parse", "--verify", "-q", name], repo).returncode == 0:
            return name
    return git(repo, "branch", "--show-current") or "main"


def package_manager(repo: Path) -> str:
    for lock, pm in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun")):
        if (repo / lock).is_file():
            return pm
    return "npm"


def python_test_cmd(repo: Path) -> str:
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8") if (repo / "pyproject.toml").is_file() else ""
    if "[tool.pytest" in pyproject or (repo / "pytest.ini").is_file() or (repo / "conftest.py").is_file():
        return "python3 -m pytest -q"
    return "python3 -m unittest discover -s tests"


# ── init ─────────────────────────────────────────────────────────────────────

def planned_files(repo: Path, stack: str) -> dict[str, str]:
    """Relative path -> content for every file the kit provides for this repo."""
    files: dict[str, str] = {}
    for base in (KIT / "common", KIT / stack):
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".tmpl"):
                rel = str(path.relative_to(base))
                if rel.startswith(".github/") and not github_remote(repo):
                    continue  # local-only repo: the git hooks are its CI equivalent
                files[rel] = path.read_text(encoding="utf-8")
    for name, rel in VENDORED_SKILLS.items():
        source = VENDOR / rel
        for path in sorted(source.rglob("*")):
            if path.is_file() and "agents" not in path.relative_to(source).parts:
                text = path.read_text(encoding="utf-8")
                if path.name == "SKILL.md" and (KIT / "overlays" / f"{name}.md").is_file():
                    text = text.rstrip("\n") + "\n" + (KIT / "overlays" / f"{name}.md").read_text(encoding="utf-8")
                files[f".claude/skills/{name}/{path.relative_to(source)}"] = text
    files[".claude/skills/VENDORED.md"] = (
        "Skills here other than implement-ticket are vendored from Matt Pocock's skills repo\n"
        f"({(VENDOR / 'SOURCE').read_text(encoding='utf-8').splitlines()[0].removeprefix('Vendored from ')})\n"
        "with local additions appended under 'Our additions'. MIT license:\n\n"
        + (VENDOR / "LICENSE").read_text(encoding="utf-8"))
    tracker = "issue-tracker-github.md" if github_remote(repo) else "issue-tracker-local.md"
    files["docs/agents/issue-tracker.md"] = (KIT / "tracker" / tracker).read_text(encoding="utf-8")
    files[".claude/slopbrake.json"] = json.dumps({"stack": stack, "kit": "slopbrake", "kit_version": kit_version()},
                                                  indent=2) + "\n"
    subs = substitutions(repo, stack)
    for rel in list(files):
        if rel in ("scripts/check", ".github/workflows/check.yml"):
            for key, value in subs.items():
                files[rel] = files[rel].replace(f"@{key}@", value)
    if stack == "typescript":
        files[".dependency-cruiser.cjs"] = (KIT / "typescript" / "dependency-cruiser.cjs.tmpl").read_text(
            encoding="utf-8").replace("@PACKAGES_ROOT@", subs["PACKAGES_ROOT"])
    pointer = (f"\n- Packages are deep modules: see [{subs['PACKAGES_ROOT']}/README.md]({subs['PACKAGES_ROOT']}/README.md)."
               if (repo / subs.get("PACKAGES_ROOT", "-") / "README.md").is_file() else "")
    files["CLAUDE.md"] = (KIT / "common" / "CLAUDE.md.tmpl").read_text(encoding="utf-8").replace(
        "@REPO@", repo.name).replace("@PACKAGES_POINTER@", pointer)
    return files


def substitutions(repo: Path, stack: str) -> dict[str, str]:
    subs = {"DEFAULT_BRANCH": default_branch(repo), "PACKAGES_ROOT": "src"}
    if stack == "python":
        subs |= {
            "TEST_CMD": python_test_cmd(repo),
            "LINT_CMD": "uvx ruff@0.16.9 check .",
            "TYPES_CMD": 'echo "types: no type checker configured for this repo yet; edit scripts/check"',
            "CI_INSTALL": "python3 -m pip install -e .",
        }
    else:
        tests = [g for g in ("'tests/**/*.ts'", "'src/**/*.test.ts'") if (repo / g.strip("'").split("/")[0]).is_dir()]
        subs |= {
            "PM": package_manager(repo),
            "DEPCRUISE_PATHS": " ".join(d for d in ("src", "tests") if (repo / d).is_dir()) or ".",
            "TEST_GLOBS": " ".join(tests) or "'**/*.test.ts'",
            "MUTATE_INCLUDE": "--include 'src/**/*.ts'",
        }
    return subs


def is_managed(rel: str) -> bool:
    return rel.startswith(MANAGED_PREFIXES) and rel != ".claude/settings.json"


def merge_settings(existing: dict, kit: dict) -> dict:
    merged = json.loads(json.dumps(existing))
    for event, entries in kit.get("hooks", {}).items():
        target = merged.setdefault("hooks", {}).setdefault(event, [])
        present = {h.get("command") for entry in target for h in entry.get("hooks", [])}
        for entry in entries:
            if not all(h.get("command") in present for h in entry["hooks"]):
                target.append(entry)
    return merged


def init(repo: Path, stack: str, update: bool, dry_run: bool) -> dict:
    files = planned_files(repo, stack)
    actions: list[Action] = []
    for rel, content in sorted(files.items()):
        target = repo / rel
        if rel == ".claude/settings.json" and target.is_file():
            merged = merge_settings(json.loads(target.read_text(encoding="utf-8")), json.loads(content))
            new = json.dumps(merged, indent=2) + "\n"
            if json.loads(target.read_text(encoding="utf-8")) == merged:
                actions.append(Action(rel, "unchanged"))
            else:
                actions.append(Action(rel, "merge", "added the kit's hooks to existing settings"))
                if not dry_run:
                    target.write_text(new, encoding="utf-8")
            continue
        if target.is_file():
            if target.read_text(encoding="utf-8") == content:
                actions.append(Action(rel, "unchanged"))
            elif update and is_managed(rel):
                actions.append(Action(rel, "update"))
                if not dry_run:
                    target.write_text(content, encoding="utf-8")
                    target.chmod(0o755 if content.startswith("#!") else 0o644)
            else:
                note = "repo-owned; review against the kit by hand" if not is_managed(rel) else "differs; --update refreshes it"
                actions.append(Action(rel, "kept", note))
            continue
        actions.append(Action(rel, "create"))
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            if content.startswith("#!"):
                target.chmod(0o755)
    ignore_file = repo / ".gitignore"
    ignored = ignore_file.read_text(encoding="utf-8").splitlines() if ignore_file.is_file() else []
    missing = [entry for entry in GITIGNORE[stack] if entry not in ignored]
    if missing:
        actions.append(Action(".gitignore", "merge", "ignore " + ", ".join(missing)))
        if not dry_run:
            prefix = "" if not ignored or ignore_file.read_text(encoding="utf-8").endswith("\n") else "\n"
            with ignore_file.open("a", encoding="utf-8") as handle:
                handle.write(prefix + "".join(entry + "\n" for entry in missing))
    hooks_path = git(repo, "config", "--get", "core.hooksPath", check=False)
    live_hooks = [p.name for p in (repo / ".git" / "hooks").glob("*") if not p.name.endswith(".sample")] \
        if (repo / ".git" / "hooks").is_dir() else []
    hooks_note = "core.hooksPath already .githooks"
    if hooks_path != ".githooks":
        if hooks_path or live_hooks:
            hooks_note = f"left alone: existing hooks ({hooks_path or ', '.join(live_hooks)}); chain .githooks by hand"
        else:
            hooks_note = "set core.hooksPath=.githooks"
            if not dry_run:
                git(repo, "config", "core.hooksPath", ".githooks")
    next_steps = []
    claude_md = repo / "CLAUDE.md"
    if any(a.path == "CLAUDE.md" and a.action == "kept" for a in actions):
        next_steps.append("CLAUDE.md exists: merge the kit's pointers into it by hand and keep it within "
                          f"{CLAUDE_MD_MAX_LINES} lines ({len(claude_md.read_text().splitlines())} now)")
    if stack == "typescript":
        pm = package_manager(repo)
        next_steps += [
            f"{pm} add -D dependency-cruiser @stryker-mutator/core @stryker-mutator/vitest-runner",
            ("wire the tautology rule into eslint.config.js: import slopbrake from './eslint-rules/slopbrake.mjs' and "
            "add { files: [<test globs>], plugins: { slopbrake }, rules: { 'slopbrake/no-tautological-test': 'error' } }"),
        ]
    if stack == "python" and "no type checker configured" in (repo / "scripts/check").read_text(encoding="utf-8"):
        next_steps.append("choose a type checker for the types stage in scripts/check (it prints 'not configured' until then)")
    next_steps.append(f"prove it: slopbrake verify {repo}")
    return {"repo": str(repo), "stack": stack, "dry_run": dry_run, "hooks": hooks_note,
            "actions": [asdict(a) for a in actions], "next_steps": next_steps}


# ── status ───────────────────────────────────────────────────────────────────

def status(repo: Path) -> dict:
    present = {rel: (repo / rel).exists() for rel in SPEC_FILES}
    claude_lines = len((repo / "CLAUDE.md").read_text(encoding="utf-8").splitlines()) if present["CLAUDE.md"] else None
    workflows = list((repo / ".github" / "workflows").glob("*.y*ml")) if (repo / ".github" / "workflows").is_dir() else []
    ci_runs_check = any("scripts/check" in w.read_text(encoding="utf-8") for w in workflows)
    hooks_path = git(repo, "config", "--get", "core.hooksPath", check=False)
    gaps = [f"missing {rel}" for rel, ok in present.items() if not ok]
    if claude_lines is not None and claude_lines > CLAUDE_MD_MAX_LINES:
        gaps.append(f"CLAUDE.md has {claude_lines} lines (max {CLAUDE_MD_MAX_LINES})")
    if hooks_path != ".githooks" and not ci_runs_check:
        gaps.append("no guardrail: neither git hooks nor CI run scripts/check")
    return {"repo": str(repo), "files": present, "claude_md_lines": claude_lines, "hooks_path": hooks_path or None,
            "ci_runs_check": ci_runs_check, "gaps": gaps, "ok": not gaps}


# ── verify ───────────────────────────────────────────────────────────────────

PY_SEEDS = {
    "boundary": ("tests/test_slopbrake_seed_boundary.py", "from {top}._slopbrake_probe import value  # seeded deep import\n"),
    "tautology": ("tests/test_slopbrake_seed_tautology.py",
                  ("def test_seeded_tautology():\n    items = [10, 5]\n    assert sum(items) == sum(items)\n"
                  "    assert True\n")),
    "mutation_src": ("{srcdir}slopbrake_seed.py",
                     ("def discount(total, member):\n    if member and total >= 100:\n        return total - 10\n"
                     "    return total\n")),
    "mutation_weak": ("tests/test_slopbrake_seed_mutation.py",
                      ("import unittest\n\nfrom slopbrake_seed import discount\n\n\nclass Seed(unittest.TestCase):\n"
                      "    def test_discount_runs(self):\n        self.assertIsNotNone(discount(150, True))\n")),
    "mutation_strong": ("tests/test_slopbrake_seed_mutation.py",
                        ("import unittest\n\nfrom slopbrake_seed import discount\n\n\nclass Seed(unittest.TestCase):\n"
                        "    def test_members_save_ten_from_one_hundred(self):\n"
                        "        self.assertEqual(discount(150, True), 140)\n        self.assertEqual(discount(100, True), 90)\n"
                        "        self.assertEqual(discount(99, True), 99)\n        self.assertEqual(discount(150, False), 150)\n")),
    "red": ("tests/test_slopbrake_seed_red.py",
            ("import unittest\n\n\nclass Seed(unittest.TestCase):\n    def test_red(self):\n"
            "        self.fail('seeded red test')\n")),
}
TS_SEEDS = {
    "boundary_impl": ("src/{pkg}/lib/slopbrake-probe.ts", "export const probe = 1;\n"),
    "boundary": ("tests/slopbrake-seed-boundary.test.ts",
                 ('import { expect, test } from "vitest";\nimport { probe } from "../src/{pkg}/lib/slopbrake-probe.js";\n\n'
                 'test("seeded deep import", () => {\n  expect(probe).toBeGreaterThan(0);\n});\n')),
    "tautology": ("tests/slopbrake-seed-tautology.test.ts",
                  ('import { expect, test } from "vitest";\n\ntest("seeded tautology", () => {\n'
                  "  const items = [10, 5];\n  expect(items.reduce((a, b) => a + b, 0)).toBe(items.reduce((a, b) => a + b, 0));\n"
                  "  expect(true).toBe(true);\n});\n")),
    "mutation_src": ("src/slopbrake-seed.ts",
                     ("export function discount(total: number, member: boolean): number {\n"
                     "  if (member && total >= 100) {\n    return total - 10;\n  }\n  return total;\n}\n")),
    "mutation_weak": ("tests/slopbrake-seed-mutation.test.ts",
                      ('import { expect, test } from "vitest";\nimport { discount } from "../src/slopbrake-seed.js";\n\n'
                      'test("discount runs", () => {\n  expect(discount(150, true)).toBeDefined();\n});\n')),
    "mutation_strong": ("tests/slopbrake-seed-mutation.test.ts",
                        ('import { expect, test } from "vitest";\nimport { discount } from "../src/slopbrake-seed.js";\n\n'
                        'test("members save ten from one hundred", () => {\n  expect(discount(150, true)).toBe(140);\n'
                        "  expect(discount(100, true)).toBe(90);\n  expect(discount(99, true)).toBe(99);\n"
                        "  expect(discount(150, false)).toBe(150);\n});\n")),
    "red": ("tests/slopbrake-seed-red.test.ts",
            ('import { expect, test } from "vitest";\nimport { discount } from "../src/slopbrake-seed.js";\n\n'
            'test("seeded red", () => {\n  expect(discount(1, false)).toBe(2);\n});\n')),
}
MIGRATION_DIFF = """diff --git a/migrations/0099_drop_accounts.sql b/migrations/0099_drop_accounts.sql
new file mode 100644
--- /dev/null
+++ b/migrations/0099_drop_accounts.sql
@@ -0,0 +1 @@
+DROP TABLE accounts;
"""
README_DIFF = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# Teh project
+# The project
"""
PR_BODY = """## Summary

```text
discount(total, member)
  member and total >= 100 -> total - 10
```

## Evidence

- **Before:** `test_members_save_ten` failed: expected 140, got 150
  **After:** 4 tests pass

## Merge Danger

**Door:** {door}

{rollback}

**Blast Radius:** checkout

## Review log

- none

## Open questions

- none
"""
GIT_GUARD_CASES = [("git push --force origin main", 2), ("git reset --hard HEAD~1", 2), ("git clean -fd", 2),
                   ("git branch -D old", 2), ("git checkout .", 2), ("git restore .", 2),
                   ("git push origin feature/x", 0), ("git status", 0), ("git restore --staged .", 0)]


class Verifier:
    def __init__(self, repo: Path, keep: bool):
        self.repo, self.keep = repo, keep
        self.stack = json.loads((repo / ".claude/slopbrake.json").read_text())["stack"] \
            if (repo / ".claude/slopbrake.json").is_file() else detect_stack(repo)
        self.proofs: list[dict] = []
        self.scratch = Path(tempfile.mkdtemp(prefix="slopbrake-verify-"))
        self.wt = self.scratch / "wt"
        self.head = git(repo, "rev-parse", "HEAD")

    def __enter__(self):
        git(self.repo, "worktree", "add", "--detach", str(self.wt), self.head)
        if (self.repo / "package.json").is_file():
            # A real install from the package manager's store; package managers reject a
            # symlinked node_modules and try to reinstall mid-check.
            pm = package_manager(self.repo)
            install = {"pnpm": "pnpm install --frozen-lockfile --prefer-offline",
                       "yarn": "yarn install --frozen-lockfile --prefer-offline",
                       "bun": "bun install --frozen-lockfile", "npm": "npm ci --prefer-offline"}[pm]
            result = run(install, self.wt)
            if result.returncode != 0:
                raise RuntimeError(f"{install} failed in the verify worktree:\n{result.stdout}{result.stderr}")
        return self

    def __exit__(self, *exc):
        if not self.keep:
            git(self.repo, "worktree", "remove", "--force", str(self.wt), check=False)
            git(self.repo, "worktree", "prune", check=False)
            shutil.rmtree(self.scratch, ignore_errors=True)

    def env(self, **extra: str) -> dict[str, str]:
        return dict(os.environ, SLOPBRAKE_BASE=self.head, CLAUDE_PROJECT_DIR=str(self.wt), **extra)

    def check(self, *stages: str, **env: str) -> subprocess.CompletedProcess:
        return run(["scripts/check", *stages], self.wt, env=self.env(**env))

    def prove(self, name: str, expect_ok: bool, result: subprocess.CompletedProcess, rule: str,
              says: str | None = None) -> bool:
        """A failure only counts when the check says why (`says`), so a crash can't pass a proof."""
        ok = (result.returncode == 0) == expect_ok and (says is None or says in result.stdout + result.stderr)
        tail = (result.stdout + result.stderr).strip().splitlines()[-6:]
        self.proofs.append({"proof": name, "rule": rule, "expected": ("pass" if expect_ok else "fail")
                            + (f" saying {says!r}" if says else ""),
                            "exit": result.returncode, "ok": ok, "output": tail})
        return ok

    def write(self, rel: str, text: str) -> Path:
        path = self.wt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def seeds(self) -> dict[str, tuple[str, str]]:
        if self.stack == "python":
            sys.path.insert(0, str(self.wt / "scripts/slopbrake"))
            from boundaries_py import Repo  # type: ignore
            tops = sorted(Repo(self.wt).tops)
            srcdir = "src/" if (self.wt / "src").is_dir() else ""
            fill = {"top": tops[0] if tops else "", "srcdir": srcdir}
            return {k: (p.format(**fill), t.format(**fill)) for k, (p, t) in PY_SEEDS.items()}
        pkgs = sorted(p.name for p in (self.wt / "src").iterdir() if p.is_dir()) if (self.wt / "src").is_dir() else []
        fill = {"pkg": pkgs[0] if pkgs else "slopbrakepkg"}
        return {k: (p.replace("{pkg}", fill["pkg"]), t.replace("{pkg}", fill["pkg"])) for k, (p, t) in TS_SEEDS.items()}

    def run_all(self) -> None:
        seeds = self.seeds()
        # Clean tree: the full gate passes (mutation skips: nothing changed since HEAD).
        self.prove("clean tree passes scripts/check", True, self.check(), "L1")

        # D1: pass -> fail on a seeded deep import -> pass after reverting.
        added = [self.write(*seeds["boundary"])]
        if "boundary_impl" in seeds:
            added.append(self.write(*seeds["boundary_impl"]))
        self.prove("seeded deep import fails boundaries", False, self.check("boundaries"), "D1",
                   "tests-through-entrypoints" if self.stack == "python" else "entrypoint-boundary")
        for path in added:
            path.unlink()
        self.prove("boundaries pass again after revert", True, self.check("boundaries"), "D1")

        # T1: a seeded tautological test fails the gate.
        seeded = self.write(*seeds["tautology"])
        self.prove("seeded tautological test fails test-quality", False, self.check("test-quality"), "T1",
                   "no-tautological-test" if self.stack == "typescript" else "tautological assertion")
        seeded.unlink()

        # T4: weak tests on changed code fall below the floor; strong tests clear it.
        self.write(*seeds["mutation_src"])
        self.write(*seeds["mutation_weak"])
        self.prove("assertion-free test fails the mutation floor", False, self.check("mutation"), "T4",
                   "below floor" if self.stack == "python" else "Final mutation score")
        self.write(*seeds["mutation_strong"])
        self.prove("real assertions clear the mutation floor", True, self.check("mutation"), "T4")

        # Stop hook: red code blocks the stop (exit 2); a green tree lets it through.
        red = self.write(*seeds["red"])
        hook = self.wt / ".claude/hooks/require-green.sh"
        stop_input = json.dumps({"session_id": "verify", "stop_hook_active": False})
        result = subprocess.run([str(hook)], input=stop_input, cwd=self.wt, env=self.env(), capture_output=True, text=True, check=False)
        self.prove("Stop hook blocks on a red gate", False, result, "H6", "FAIL  tests")
        red.unlink()
        result = subprocess.run([str(hook)], input=stop_input, cwd=self.wt, env=self.env(), capture_output=True, text=True, check=False)
        self.prove("Stop hook allows a green gate", True, result, "H6")
        for key in ("mutation_src", "mutation_weak"):
            (self.wt / seeds[key][0]).unlink(missing_ok=True)

        # G2: a fake migration is one-way; a README typo is two-way.
        classify = self.wt / "scripts/slopbrake/door_classify.py"
        for name, diff, door in (("migration", MIGRATION_DIFF, "one-way"), ("readme typo", README_DIFF, "two-way")):
            diff_file = self.scratch / f"{name.replace(' ', '-')}.diff"
            diff_file.write_text(diff, encoding="utf-8")
            result = run([sys.executable, str(classify), "--diff-file", str(diff_file), "--json"], self.wt)
            got = json.loads(result.stdout)["door"] if result.returncode == 0 else f"error: {result.stderr}"
            fake = subprocess.CompletedProcess([], 0 if got == door else 1, f"classified {got}", "")
            self.prove(f"{name} diff classifies {door}", True, fake, "G2")

        # G1/G2/G4: PR body shape and the door floor, against a committed migration.
        self.write("migrations/9999_slopbrake_seed.sql", "DELETE FROM accounts;\n")
        run(["git", "add", "-A"], self.wt, check=True)
        run(["git", "-c", "user.name=slopbrake-verify", "-c", "user.email=verify@localhost", "commit", "-q",
             "--no-verify", "-m", "seed migration"], self.wt, check=True)
        body = self.scratch / "pr.md"
        checker = ["python3", "scripts/slopbrake/pr_body_check.py", "--body-file", str(body)]
        body.write_text(PR_BODY.format(door="one-way", rollback="Rollback: restore the accounts backup."), encoding="utf-8")
        self.prove("complete one-way PR body passes", True, run(checker, self.wt, env=self.env()), "G1/G4")
        body.write_text(PR_BODY.format(door="two-way", rollback=""), encoding="utf-8")
        self.prove("under-declared door fails (two-way on a migration)", False, run(checker, self.wt, env=self.env()), "G2",
                   "never lowered")
        body.write_text(PR_BODY.format(door="one-way", rollback="").replace("**Door:** one-way", ""), encoding="utf-8")
        self.prove("PR body without Door fails", False, run(checker, self.wt, env=self.env()), "G1",
                   "exactly one '**Door:**")

        # H5: destructive git commands are blocked; branch pushes are not.
        guard = self.wt / ".claude/hooks/block-dangerous-git.sh"
        wrong = []
        for command, expected in GIT_GUARD_CASES:
            result = subprocess.run([str(guard)], input=json.dumps({"tool_input": {"command": command}}),
                                    capture_output=True, text=True, check=False)
            if result.returncode != expected:
                wrong.append(f"{command}: exit {result.returncode}, expected {expected}")
        self.prove("git guard blocks destructive commands only", True,
                   subprocess.CompletedProcess([], 1 if wrong else 0, "\n".join(wrong) or "all cases ok", ""), "H5")

        # H1: CLAUDE.md stays a short pointer file.
        lines = (self.repo / "CLAUDE.md").read_text(encoding="utf-8").splitlines() if (self.repo / "CLAUDE.md").is_file() else []
        short = 0 < len(lines) <= CLAUDE_MD_MAX_LINES
        self.prove(f"CLAUDE.md is at most {CLAUDE_MD_MAX_LINES} lines", True,
                   subprocess.CompletedProcess([], 0 if short else 1, f"{len(lines)} lines", ""), "H1")


def verify(repo: Path, keep: bool) -> dict:
    if run(["git", "status", "--porcelain"], repo).stdout.strip():
        note = "working tree has uncommitted changes; verify proves the committed HEAD only"
    else:
        note = ""
    with Verifier(repo, keep) as verifier:
        if not (verifier.wt / "scripts/check").is_file():
            verifier.proofs.append({"proof": "Slopbrake is committed at HEAD", "rule": "L1", "ok": False,
                                    "output": ["HEAD has no scripts/check: commit the files `slopbrake init` wrote, then verify"]})
            return {"repo": str(repo), "stack": verifier.stack, "head": verifier.head, "note": note,
                    "worktree": None, "proofs": verifier.proofs, "ok": False}
        try:
            verifier.run_all()
        except Exception as exc:  # noqa: BLE001 - any harness error becomes a failed proof, not a traceback
            verifier.proofs.append({"proof": "verify harness", "ok": False, "output": [repr(exc)]})
    return {"repo": str(repo), "stack": verifier.stack, "head": verifier.head, "note": note,
            "worktree": str(verifier.wt) if keep else None, "proofs": verifier.proofs,
            "ok": all(p["ok"] for p in verifier.proofs)}


# ── cli ──────────────────────────────────────────────────────────────────────

def print_human(command: str, result) -> None:
    if command == "init":
        for a in result["actions"]:
            if a["action"] != "unchanged":
                print(f"{a['action']:>9}  {a['path']}" + (f"  ({a['note']})" if a["note"] else ""))
        print(f"    hooks  {result['hooks']}")
        for step in result["next_steps"]:
            print(f"     next  {step}")
    elif command == "status":
        for r in result:
            print(f"{'ok' if r['ok'] else 'GAPS'}  {r['repo']}")
            for gap in r["gaps"]:
                print(f"      - {gap}")
    else:
        for p in result["proofs"]:
            print(f"{'ok  ' if p['ok'] else 'FAIL'}  [{p.get('rule', '')}] {p['proof']}")
            if not p["ok"]:
                for line in p.get("output", []):
                    print(f"        {line}")
        if result["note"]:
            print(f"note: {result['note']}")
        print("verify: all proofs hold" if result["ok"] else "verify: some proofs FAILED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slopbrake", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="install or refresh the kit in a repo")
    p_init.add_argument("repo", type=Path)
    p_init.add_argument("--stack", choices=["auto", "python", "typescript"], default="auto")
    p_init.add_argument("--update", action="store_true", help="refresh kit-managed files that differ")
    p_init.add_argument("--dry-run", action="store_true")
    p_status = sub.add_parser("status", help="which spec files and guardrails a repo has")
    p_status.add_argument("repos", type=Path, nargs="+")
    p_verify = sub.add_parser("verify", help="prove the checks bite, in a throwaway worktree")
    p_verify.add_argument("repo", type=Path)
    p_verify.add_argument("--keep", action="store_true", help="keep the worktree for inspection")
    for p in (p_init, p_status, p_verify):
        p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "init":
        repo = args.repo.resolve()
        result = init(repo, detect_stack(repo) if args.stack == "auto" else args.stack, args.update, args.dry_run)
        ok = True
    elif args.command == "status":
        result = [status(r.resolve()) for r in args.repos]
        ok = all(r["ok"] for r in result)
    else:
        result = verify(args.repo.resolve(), args.keep)
        ok = result["ok"]
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_human(args.command, result)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
