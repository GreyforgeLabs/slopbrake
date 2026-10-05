#!/usr/bin/env python3
"""slopbrake: put brakes on coding agents: install the checks into a repo and prove they bite.

  slopbrake init REPO [--stack auto|python|typescript] [--update] [--dry-run] [--json]
  slopbrake status REPO... [--json]
  slopbrake verify REPO [--json] [--keep]
  slopbrake user-hooks install|uninstall|status [--harness claude|codex|opencode] [--settings PATH] [--json]
  slopbrake user-hooks trust|untrust REPO           (the Stop gate runs only trusted repos' scripts)
  slopbrake-hook pre-tool-use|post-tool-use|stop     (Claude Code runs this; hook JSON on stdin;
                                                      `slopbrake hook` is an alias)

init writes two kinds of files. Managed files (scripts/slopbrake, hooks, the reviewer
agent, vendored skills) belong to the kit: --update refreshes them. Seeded files
(CLAUDE.md, CODING_STANDARDS.md, door rules, scripts/check, retro log, CI) belong to the
repo once written: init never overwrites them. verify runs a proof for each rule
in a throwaway git worktree, so the repo's working tree is never touched. user-hooks wires
the git guard and Stop gate into ~/.claude/settings.json for sessions started elsewhere.
Exit codes: 0 ok, 1 a check or proof failed, 2 usage or environment error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from slopbrake import __version__, harness, hooks

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
GITIGNORE = {"python": ["__pycache__/", ".ruff_cache/", ".mypy_cache/"], "typescript": [".stryker-tmp/", "reports/", "__pycache__/"]}
LAST_GREEN = "refs/slopbrake/last-green"  # + /<branch, "/" as %2F>; detached HEAD has none
META = ".claude/slopbrake.json"
HOOKS_DISABLED = "/dev/null"  # core.hooksPath that turns every git hook off
TYPES_PLACEHOLDER = "no type checker configured"
ESLINT_CONFIGS = [f"eslint.config.{ext}" for ext in ("js", "mjs", "cjs", "ts", "mts", "cts")] + [
    f".eslintrc{ext}" for ext in ("", ".js", ".cjs", ".json", ".yml", ".yaml")]
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "env", "build", "dist", "site-packages"}
SUBDIR_UNSUPPORTED = "monorepo subdirectories are not supported yet"
# Inherited PR context and base pins that would steer verify's proofs away from the seeded changes (B15).
VERIFY_SCRUB = ("GITHUB_BASE_REF", "GITHUB_EVENT_PATH", "PR_BODY_FILE", "SLOPBRAKE_BASE")


class UsageError(Exception):
    """A usage or environment problem: exit 2 with one line, never a traceback."""


@dataclass
class Action:
    path: str
    action: str  # create | update | unchanged | kept | merge | delete
    note: str = ""


def run(cmd, cwd: Path, env=None, check=False, stdin: str | None = None) -> subprocess.CompletedProcess:
    """cmd in its own process group: an interrupted verify (B12) also stops the gate's test runners and mutants."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=isinstance(cmd, str),
                            start_new_session=True)
    try:
        out, err = proc.communicate(stdin)
    except BaseException:
        stop_group(proc)
        raise
    result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check and result.returncode != 0:
        raise RuntimeError(f"{cmd if isinstance(cmd, str) else ' '.join(cmd)} failed in {cwd}: {last_line(result)}")
    return result


def stop_group(proc: subprocess.Popen) -> None:
    """TERM the process group, give it 10 s to clean up (mutants restore files, runners remove temp dirs), then KILL."""
    deadline, sig = time.monotonic() + 10, signal.SIGTERM
    while True:
        try:
            os.killpg(proc.pid, sig)  # signal 0 only asks whether any member is left
        except ProcessLookupError:
            break
        if sig == signal.SIGKILL:
            break
        try:
            proc.wait(timeout=0.1)  # reap the leader, or the group never empties
        except subprocess.TimeoutExpired:
            pass
        sig = 0 if time.monotonic() < deadline else signal.SIGKILL
    proc.wait()


def last_line(result: subprocess.CompletedProcess) -> str:
    lines = (result.stderr.strip() or result.stdout.strip()).splitlines()
    return lines[-1] if lines else f"exit {result.returncode}"


def cause_line(result: subprocess.CompletedProcess) -> str:
    """The first message that says why a tool failed, rejoined when it wraps over several lines
    (npm leads with codes, ends with a log path and separates messages with a bare prefix line)."""
    message: list[str] = []
    for line in (result.stderr.strip() or result.stdout.strip()).splitlines():
        text = re.sub(r"^(npm (error|ERR!)|error)\s*", "", line.strip())
        if text and not re.match(r"code \S+$|A complete log", text):
            message.append(line.strip() if not message else text)
        elif message:
            break
    return " ".join(message) or last_line(result)


def git(repo: Path, *args: str, check=True) -> str:
    return run(["git", *args], repo, check=check).stdout.strip()


def kit_version() -> str:
    """Package version, plus the commit when running from a git checkout of the kit."""
    sha = git(HOME, "rev-parse", "--short", "HEAD", check=False) if (HOME.parent / ".git").exists() else ""
    return f"{__version__}+{sha}" if sha else __version__


# ── detection ────────────────────────────────────────────────────────────────

def git_toplevel(path: Path) -> Path:
    if not path.is_dir():
        raise UsageError(f"{path}: no such directory")
    result = run(["git", "rev-parse", "--show-toplevel"], path)
    if result.returncode != 0:
        raise UsageError(f"{path} is not inside a git repository (git init first)")
    return Path(result.stdout.strip()).resolve()


def repo_root(path: Path) -> Path:
    """The repository root `path` names; a subdirectory is a usage error (B8)."""
    top = git_toplevel(path)
    if path.resolve() != top:
        raise UsageError(f"slopbrake installs at a repository root ({top}); {SUBDIR_UNSUPPORTED}")
    return top


def detect_stack(repo: Path) -> str:
    if (repo / "package.json").is_file() and (repo / "tsconfig.json").is_file():
        return "typescript"
    if (repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file():
        return "python"
    raise UsageError(f"cannot detect a supported stack in {repo} (python, typescript); pass --stack")


def recorded_stack(repo: Path) -> str | None:
    """The stack init recorded, which wins over detection (a python repo may also have a package.json)."""
    try:
        stack = json.loads((repo / META).read_text(encoding="utf-8")).get("stack")
    except (OSError, ValueError, AttributeError):
        return None
    return stack if stack in ("python", "typescript") else None


def github_remote(repo: Path) -> bool:
    return "github.com" in git(repo, "remote", "-v", check=False)


def default_branch(repo: Path) -> str | None:
    """The kit's rule (common.default_branch, B16), mirrored because cli cannot import the kit:
    origin/HEAD's target, else init.defaultBranch when that branch exists, else main, else master."""
    origin = git(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False)
    if origin:
        return origin.split("/", 1)[1]
    configured = git(repo, "config", "init.defaultBranch", check=False)
    for name in ([configured] if configured else []) + ["main", "master"]:
        if run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{name}"], repo).returncode == 0:
            return name
    return None


def current_branch(repo: Path) -> str:
    return git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False)


def ratchet_ref(branch: str) -> str:
    """One flat ref per branch, "/" encoded as %2F, so feature and feature/x never D/F-conflict (B1)."""
    return f"{LAST_GREEN}/{branch.replace('/', '%2F')}"


def last_green_ref(repo: Path) -> str | None:
    """The current branch's last-green ratchet; None on a detached HEAD (never recorded or used)."""
    branch = current_branch(repo)
    return ratchet_ref(branch) if branch else None


def commit_paths(repo: Path, paths: list[str], pending: list[str]) -> list[str]:
    """Kit paths with uncommitted (or, in a dry run, `pending`) changes, kit-owned directories
    collapsed, for a `git add` that sweeps in nothing else."""
    prefix = git(repo, "rev-parse", "--show-prefix", check=False)  # porcelain paths are toplevel-relative
    dirty = run(["git", "status", "--porcelain", "-z", "--untracked-files=all", "--", *paths], repo).stdout
    found = {entry[3:].removeprefix(prefix) for entry in dirty.split("\0") if len(entry) > 3} | set(pending)
    out: dict[str, None] = {}
    for rel in paths:
        if rel not in found:
            continue
        for owned in ("scripts/slopbrake/", ".claude/hooks/", ".githooks/"):
            if rel.startswith(owned):
                rel = owned.rstrip("/")
        if rel.startswith(".claude/skills/"):
            rel = "/".join(rel.split("/")[:3])
        out[rel] = None
    return sorted(out)


def package_manager(repo: Path) -> str:
    for lock, pm in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun")):
        if (repo / lock).is_file():
            return pm
    return "npm"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def pyproject(repo: Path) -> dict:
    try:
        return tomllib.loads(read_text(repo / "pyproject.toml"))
    except ValueError:
        return {}


def declared_requirements(data: dict) -> list[str]:
    """Requirement strings and names from every dependency list a pyproject can declare."""
    project, tool = data.get("project", {}), data.get("tool", {})
    lists = [project.get("dependencies", []), *project.get("optional-dependencies", {}).values(),
             *data.get("dependency-groups", {}).values(), tool.get("uv", {}).get("dev-dependencies", [])]
    poetry = tool.get("poetry", {})
    names = [*poetry.get("dependencies", {}), *poetry.get("dev-dependencies", {}),
             *(name for group in poetry.get("group", {}).values() for name in group.get("dependencies", {}))]
    return [req for reqs in lists for req in reqs if isinstance(req, str)] + names


def uses_pytest(repo: Path) -> bool:
    if "[tool.pytest" in read_text(repo / "pyproject.toml") or (repo / "pytest.ini").is_file():
        return True
    if "[pytest]" in read_text(repo / "tox.ini") or "[tool:pytest]" in read_text(repo / "setup.cfg"):
        return True
    for _, dirs, files in os.walk(repo):
        if "conftest.py" in files:
            return True
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
    return any(re.split(r"[\s\[<>=!~;@(]", req.strip(), maxsplit=1)[0].lower() == "pytest"
               for req in declared_requirements(pyproject(repo)))


def python_test_args(repo: Path) -> str:
    """The test command's arguments: pytest when the repo configures or declares it anywhere (it runs
    unittest tests too). scripts/check picks the interpreter at run time (B6), so worktrees work."""
    return "-m pytest -q" if uses_pytest(repo) else "-m unittest discover -s tests"


def has_tests(repo: Path, stack: str) -> bool:
    name = re.compile(r"(test.*|.*_test)\.py" if stack == "python" else r".*\.(test|spec)\.[cm]?[jt]sx?")
    for _, dirs, files in os.walk(repo):
        if any(name.fullmatch(f) for f in files):
            return True
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
    return False


def python_ci_install(repo: Path, test_cmd: str) -> str:
    if (repo / "uv.lock").is_file():
        return "uv sync --frozen --all-extras --all-groups"
    extras = [e for e in ("dev", "test") if e in pyproject(repo).get("project", {}).get("optional-dependencies", {})]
    target = "'.[" + ",".join(extras) + "]'" if extras else "."
    pip = f"-m pip install -e {target}" + (" pytest" if "-m pytest" in test_cmd else "")
    return f"python3 -m venv .venv && .venv/bin/python {pip}" if (repo / ".venv").is_dir() else f"python3 {pip}"


def package_json(repo: Path) -> dict:
    try:
        package = json.loads(read_text(repo / "package.json") or "{}")
    except ValueError:
        return {}
    return package if isinstance(package, dict) else {}


def package_scripts(repo: Path) -> dict:
    scripts = package_json(repo).get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def ts_layout(repo: Path) -> tuple[list[str], list[str]]:
    """(source roots, test dirs) as globs, from whichever of src/, lib/, packages/*/src and tests/,
    test/, __tests__ exist (B7); src/ and tests/ when none does."""
    sources = [d for d in ("src", "lib") if (repo / d).is_dir()]
    if any(p.is_dir() for p in repo.glob("packages/*/src")):
        sources.append("packages/*/src")
    tests = [d for d in ("tests", "test", "__tests__") if (repo / d).is_dir()]
    return sources or ["src"], tests or ["tests"]


def node_ci(repo: Path, pm: str) -> tuple[str, str]:
    """(CI_SETUP, CI_INSTALL) for the TypeScript workflow: step items indented 6 spaces, one command."""
    package = package_json(repo)
    engines = package.get("engines")
    version = next((f"node-version-file: {name}" for name, ok in (
        ("package.json", isinstance(engines, dict) and bool(engines.get("node"))),
        (".nvmrc", (repo / ".nvmrc").is_file()), (".node-version", (repo / ".node-version").is_file())) if ok),
        "node-version: 22")
    node = ["- uses: actions/setup-node@v4", "  with:", f"    {version}", f"    cache: {pm}"]
    pnpm = ["- uses: pnpm/action-setup@v4"] + ([] if package.get("packageManager") else ["  with:", "    version: 10"])
    steps = {"pnpm": pnpm + node, "npm": node, "yarn": ["- run: corepack enable", *node],
             "bun": ["- uses: oven-sh/setup-bun@v2"]}[pm]
    install = {"pnpm": "pnpm install --frozen-lockfile", "npm": "npm ci", "yarn": "yarn install --frozen-lockfile",
               "bun": "bun install --frozen-lockfile"}[pm]
    return "\n".join("      " + step for step in steps), install


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
            files[rel] = render(files[rel], subs)
    if stack == "typescript":
        files[".dependency-cruiser.cjs"] = (KIT / "typescript" / "dependency-cruiser.cjs.tmpl").read_text(
            encoding="utf-8").replace("@PACKAGES_ROOT@", subs["PACKAGES_ROOT"])
    pointer = (f"\n- Packages are deep modules: see [{subs['PACKAGES_ROOT']}/README.md]({subs['PACKAGES_ROOT']}/README.md)."
               if (repo / subs.get("PACKAGES_ROOT", "-") / "README.md").is_file() else "")
    files["CLAUDE.md"] = (KIT / "common" / "CLAUDE.md.tmpl").read_text(encoding="utf-8").replace(
        "@REPO@", repo.name).replace("@PACKAGES_POINTER@", pointer)
    return files


def render(text: str, subs: dict[str, str]) -> str:
    for key, value in subs.items():
        text = text.replace(f"@{key}@", value)
    return text


def substitutions(repo: Path, stack: str) -> dict[str, str]:
    subs = {"DEFAULT_BRANCH": default_branch(repo) or current_branch(repo) or "main", "PACKAGES_ROOT": "src"}
    if stack == "python":
        test_args = python_test_args(repo)
        subs |= {
            "TEST_ARGS": test_args,
            "LINT_CMD": "uvx ruff@0.16.9 check .",
            "TYPES_CMD": f'echo "types: skipped: {TYPES_PLACEHOLDER} (edit scripts/check)"; return 78',
            "CI_INSTALL": python_ci_install(repo, test_args),
        }
    else:
        pm = package_manager(repo)
        sources, tests = ts_layout(repo)
        present = [d for d in sources + tests if any(repo.glob(d))]
        roots = [d.replace("*", "[^/]+") for d in sources]
        ci_setup, ci_install = node_ci(repo, pm)
        subs |= {
            "PM": pm,
            "PACKAGES_ROOT": roots[0] if len(roots) == 1 else f"(?:{'|'.join(roots)})",
            "DEPCRUISE_PATHS": " ".join(dict.fromkeys(d.split("/", 1)[0] for d in present)) or ".",
            "TEST_GLOBS": " ".join(f"'{d}/**/*.{kind}.{ext}'" for d in tests + sources for kind in ("test", "spec")
                                   for ext in ("ts", "tsx")),
            "MUTATE_INCLUDE": " ".join(f"--include '{root}/**/*.{ext}'" for root in sources for ext in ("ts", "tsx")),
            "CI_SETUP": ci_setup,
            "CI_INSTALL": ci_install,
        }
    return subs


def is_managed(rel: str) -> bool:
    return rel.startswith(MANAGED_PREFIXES) and rel != ".claude/settings.json"


def merge_settings(existing: dict, kit: dict) -> dict:
    """Add each kit hook entry unless an entry with the same matcher already runs its commands."""
    merged = json.loads(json.dumps(existing))
    merged["hooks"] = merged.get("hooks") or {}
    for event, entries in kit.get("hooks", {}).items():
        target = merged["hooks"][event] = merged["hooks"].get(event) or []
        for entry in entries:
            present = {c for e in target if e.get("matcher") == entry.get("matcher") for c in hooks.commands(e)}
            if not set(hooks.commands(entry)) <= present:
                target.append(entry)
    return merged


def init(repo: Path, stack: str, update: bool, dry_run: bool) -> dict:
    repo = repo_root(repo)
    existing_settings = hooks.load_settings(repo / ".claude/settings.json")  # refuse before writing anything
    files = planned_files(repo, stack)
    actions: list[Action] = []
    for rel, content in sorted(files.items()):
        target = repo / rel
        if rel == ".claude/settings.json" and target.is_file():
            merged = merge_settings(existing_settings, json.loads(content))
            new = json.dumps(merged, indent=2) + "\n"
            if existing_settings == merged:
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
    ratchet = last_green_ref(repo)
    if ratchet and git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False) and \
            not git(repo, "rev-parse", "-q", "--verify", ratchet, check=False):
        # Older layouts block this ref: a pre-A1 flat ref, or pre-B1 refs nested under the name.
        nested = git(repo, "for-each-ref", "--format=%(refname)", f"{ratchet}/", check=False).split()
        blocking = ([LAST_GREEN] if git(repo, "rev-parse", "-q", "--verify", LAST_GREEN, check=False) else []) + nested
        actions += [Action(old, "delete", "an older ratchet layout that blocks the new ref") for old in blocking]
        actions.append(Action(ratchet, "create", "the last-green ratchet starts at HEAD"))
        if not dry_run:
            for old in blocking:
                git(repo, "update-ref", "-d", old)
            git(repo, "update-ref", ratchet, "HEAD")
    hooks_path = git(repo, "config", "--get", "core.hooksPath", check=False)
    hooks_dir = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
    live_hooks = sorted(p.name for p in hooks_dir.glob("*") if not p.name.endswith(".sample")) \
        if not hooks_path and hooks_dir.is_dir() else []
    hooks_note = "core.hooksPath already .githooks"
    next_steps = []
    if hooks_path == HOOKS_DISABLED:  # not hooks to chain into: the repo turned every git hook off
        hooks_note = f"left alone: git hooks are disabled (core.hooksPath={HOOKS_DISABLED})"
        next_steps.append(f"hooks are disabled in this repo (core.hooksPath={HOOKS_DISABLED}): enable with git config "
                          "core.hooksPath .githooks")
    elif hooks_path != ".githooks":
        if hooks_path or live_hooks:
            hooks_note = f"left alone: existing hooks ({hooks_path or ', '.join(live_hooks)}); chain .githooks by hand"
        else:
            hooks_note = "set core.hooksPath=.githooks"
            if not dry_run:
                git(repo, "config", "core.hooksPath", ".githooks")
    claude_md = repo / "CLAUDE.md"
    if any(a.path == "CLAUDE.md" and a.action == "kept" for a in actions):
        next_steps.append("CLAUDE.md exists: merge the kit's pointers into it by hand and keep it within "
                          f"{CLAUDE_MD_MAX_LINES} lines ({len(claude_md.read_text().splitlines())} now)")
    if stack == "typescript":
        pm = package_manager(repo)
        next_steps += [
            f"{pm} add -D dependency-cruiser @stryker-mutator/core @stryker-mutator/vitest-runner",
            ("wire the tautology rule into eslint.config.js: import slopbrake from './eslint-rules/slopbrake.mjs' and "
            "add { files: [<test globs>], plugins: { slopbrake }, rules: { 'slopbrake/no-tautological-test': 'error', 'slopbrake/expect-in-test': 'error' } }"),
        ]
    check_text = read_text(repo / "scripts/check") or files.get("scripts/check", "")  # a dry run wrote nothing
    if stack == "python" and TYPES_PLACEHOLDER in check_text:
        next_steps.append("choose a type checker for the types stage in scripts/check (it skips until then)")
    if types_unconfigured(repo, stack, check_text):
        next_steps.append('add a "typecheck" script to package.json (e.g. "tsc --noEmit"): the types stage skips until then')
    if not has_tests(repo, stack):
        example = "tests/test_*.py" if stack == "python" else "tests/*.test.ts"
        next_steps.append(f"add a first test ({example}): the tests stage, and so the pre-commit hook that guards "
                          "the kit's own commit, fails until one exists")
    if stack == "python" and "pytest" in python_test_args(repo) and not (repo / ".venv").is_dir() \
            and not (repo / "uv.lock").is_file():
        next_steps.append("scripts/check runs pytest with python3 when there is no .venv: create one with pytest "
                          "(python3 -m venv .venv && .venv/bin/python -m pip install -e . pytest) or make sure "
                          "python3 has pytest")
    if not dry_run:
        next_steps += trust_repo(repo)
    written = [a.path for a in actions if a.action in ("create", "update", "merge") and a.path != ratchet]
    paths = commit_paths(repo, [*files, ".gitignore"], written if dry_run else [])
    if paths:
        cd, add = f"cd {shlex.quote(str(repo))}", f"git add -- {' '.join(map(shlex.quote, paths))}"
        commit = "git commit -m 'Add slopbrake guardrails'"
        branch = current_branch(repo)
        if not git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False):  # nothing to block yet
            next_steps.append(f"commit the kit as the first commit on {branch or 'this branch'}: {cd} && {add} && {commit}")
        elif branch and branch == default_branch(repo):  # the pr stage blocks one-way doors, such as the kit, here
            next_steps.append(f"commit the kit on a feature branch (the gate blocks one-way doors on {branch}): "
                              f"{cd} && git switch -c add-slopbrake && {add} && {commit}  "
                              f"(or a human commits it on {branch} with --no-verify)")
        else:
            next_steps.append(f"commit the kit (verify proves the committed HEAD): {cd} && {add} && {commit}")
    next_steps.append(f"prove it: slopbrake verify {repo}")
    return {"repo": str(repo), "stack": stack, "dry_run": dry_run, "hooks": hooks_note,
            "actions": [asdict(a) for a in actions], "next_steps": next_steps}


def trust_repo(repo: Path) -> list[str]:
    """Let the user-level Stop gate run this repo's scripts (B11 b); a next step when that fails."""
    try:
        from slopbrake.hooks import trust  # the hooks module gained trust() in 0.2.0
    except ImportError:
        return ["user-level Stop gate: this slopbrake build cannot trust repos (slopbrake.hooks has no trust()); "
                "reinstall slopbrake, then run: slopbrake user-hooks trust " + shlex.quote(str(repo))]
    try:
        trust(repo)
    except (OSError, ValueError) as exc:
        return [f"user-level Stop gate: could not trust {repo} ({exc}); run: slopbrake user-hooks trust "
                + shlex.quote(str(repo))]
    return []


# ── status ───────────────────────────────────────────────────────────────────

def git_hook_gaps(repo: Path) -> list[str]:
    """Why git would not run the gate's pre-commit and pre-push hooks (core.hooksPath or the hooks dir)."""
    configured = git(repo, "config", "--get", "core.hooksPath", check=False)
    if configured == HOOKS_DISABLED:
        return [f"git hooks are disabled (core.hooksPath={HOOKS_DISABLED})"]
    hooks_dir = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
    if configured and not hooks_dir.is_dir():
        return [f"core.hooksPath={configured} does not exist, so no git hook runs"]
    missing = [name for name in ("pre-commit", "pre-push") if not os.access(hooks_dir / name, os.X_OK)]
    if missing:
        return [f"git hooks: no executable {' or '.join(missing)} in {hooks_dir}"]
    # Executable is not enough: a husky or lefthook setup that was never chained skips the gate (B9).
    # Husky 9 points core.hooksPath at stubs in .husky/_ that run .husky/<hook>.
    silent = [name for name in ("pre-commit", "pre-push") if not any(
        "scripts/check" in read_text(d / name) for d in (hooks_dir, *[hooks_dir.parent] * (hooks_dir.name == "_")))]
    return [(f"git hooks in {hooks_dir} do not run scripts/check ({', '.join(silent)}): chain scripts/check --fast "
             "into pre-commit and scripts/check into pre-push")] if silent else []


def wiring_gaps(repo: Path, local: Path | None = None) -> list[str]:
    """Claude Code hooks (H5/H6), git hooks and python3: everything the kit's hooks need to fire."""
    gaps = hooks.settings_gaps(repo, local) + git_hook_gaps(repo)
    return gaps if shutil.which("python3") else gaps + ["python3 is not on PATH (the hooks and the gate need it)"]


def door_rule_items(text: str) -> set[tuple[str, str]]:
    """(key, item) for every one_way glob and content pattern in a door-rules.yml (`key:` then `- item`)."""
    items, key = set(), None
    for line in text.splitlines():
        if heading := re.match(r"([A-Za-z_]+):\s*(#.*)?$", line):
            key = heading[1]
        elif key in ("one_way", "content_patterns") and (item := re.match(
                r"\s+-\s+(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^#]*[^#\s])", line)):
            value = item[1]
            items.add((key, value[1:-1] if value[:1] in "\"'" and value[-1:] == value[:1] else value))
    return items


def door_rule_gaps(repo: Path) -> list[str]:
    """Kit door rules the repo's (seeded, repo-owned) door-rules.yml lacks, e.g. after init --update."""
    rules = repo / ".claude/door-rules.yml"
    if not rules.is_file():
        return []  # reported as a missing spec file
    kit = KIT / "common/.claude/door-rules.yml"
    missing = door_rule_items(kit.read_text(encoding="utf-8")) - door_rule_items(read_text(rules))
    return [(f".claude/door-rules.yml lacks {len(missing)} kit rule(s) (one_way, content_patterns): merge them "
             f"by hand from {kit}")] if missing else []


# Seeded scripts/check lines from older kits that weaken the gate (B9, B13); comment lines don't count.
GATE_PATTERNS = [
    (re.compile(r"\$\{(MUTATION_FLOOR|TEST_CMD):?[-=]"),
     ("scripts/check takes {0} from the environment, so `{0}=...` lowers the gate: hardcode it as the kit's "
      "scripts/check does")),
    (re.compile(r"(\$PM|\$\{PM\}|\bnpm|\bpnpm) exec\b"),
     ("scripts/check runs tools through `{0} exec`, which swallows their flags and can install a squatted "
      "package: hand-merge the kit's scripts/check (node_modules/.bin tools)")),
]


def gate_gaps(check_text: str) -> list[str]:
    gaps = []
    for line in check_text.splitlines():
        if not line.lstrip().startswith("#"):
            gaps += [message.format(m[1]) for pattern, message in GATE_PATTERNS if (m := pattern.search(line))]
    return gaps


def types_unconfigured(repo: Path, stack: str, check_text: str) -> bool:
    """A TS repo whose kit types stage skips: package.json has no "typecheck" script (B13)."""
    return stack == "typescript" and "typecheck" in check_text and not package_scripts(repo).get("typecheck")


def ts_layout_gaps(repo: Path, check_text: str) -> list[str]:
    """Dirs holding TypeScript sources below no TEST_GLOBS or --include glob's base dir in scripts/check (B7)."""
    globs = re.findall(r"(?<!--exclude )'([^'\s]*\*\*[^'\s]*)'", check_text)
    bases = re.compile("|".join(re.escape(glob.split("**", 1)[0]).replace(r"\*", "[^/]*") for glob in globs) or "(?!)")
    files = run(["git", "ls-files", "-co", "--exclude-standard", "--", "*.ts", "*.tsx", "*.mts", "*.cts"], repo).stdout
    dirs = sorted({f.rsplit("/", 1)[0] + "/" for f in files.splitlines() if "/" in f and not f.endswith(".d.ts")
                   and not bases.match(f) and not any(d.startswith(".") or d in SKIP_DIRS for d in f.split("/")[:-1])})
    outside = [d for i, d in enumerate(dirs) if not any(d.startswith(parent) for parent in dirs[:i])]
    return [(f"TypeScript sources outside every scripts/check glob (no T1 or T4 there): {', '.join(outside)}; "
             "add them to TEST_GLOBS and the mutation --include list")] if outside else []


def user_hook_findings() -> tuple[list[str], list[str]]:
    """(gaps, notes) from the user's ~/.claude/settings.json: disableAllHooks turns every hook off;
    missing user-level hooks only matter for sessions started outside the repo."""
    path = hooks.default_settings()
    try:
        data = hooks.load_settings(path)
    except hooks.SettingsError as exc:
        return [], [f"cannot read user settings: {exc}"]
    gaps = [f"{path} sets disableAllHooks: Claude Code runs no hooks at all"] if data.get("disableAllHooks") else []
    installed = all(any(hooks.ours(c) for entry in hooks.hook_entries(data, event) for c in hooks.commands(entry))
                    for event in hooks.USER_ENTRIES)
    notes = [] if installed else [("user-level hooks are not installed (sessions started outside this repo get no "
                                   "git guard or Stop gate): slopbrake user-hooks install")]
    return gaps, notes


def stale_files(repo: Path, stack: str) -> list[str]:
    """Kit-managed files that are missing or differ from what init would write now."""
    return [rel for rel, content in planned_files(repo, stack).items()
            if is_managed(rel) and rel != META and read_text(repo / rel) != content]


def status(repo: Path) -> dict:
    present = {rel: (repo / rel).exists() for rel in SPEC_FILES}
    claude_lines = len((repo / "CLAUDE.md").read_text(encoding="utf-8").splitlines()) if present["CLAUDE.md"] else None
    workflows = list((repo / ".github" / "workflows").glob("*.y*ml")) if (repo / ".github" / "workflows").is_dir() else []
    ci_runs_check = any("scripts/check" in w.read_text(encoding="utf-8") for w in workflows)
    hooks_path = git(repo, "config", "--get", "core.hooksPath", check=False)
    gaps = [f"missing {rel}" for rel, ok in present.items() if not ok]
    if claude_lines is not None and claude_lines > CLAUDE_MD_MAX_LINES:
        gaps.append(f"CLAUDE.md has {claude_lines} lines (max {CLAUDE_MD_MAX_LINES})")
    if git_hook_gaps(repo) and not ci_runs_check:
        gaps.append("no guardrail: neither git hooks nor CI run scripts/check")
    gaps += wiring_gaps(repo)
    stack, stale = recorded_stack(repo), []
    if stack:
        installed = str(json.loads((repo / META).read_text(encoding="utf-8")).get("kit_version", "unknown"))
        if installed.split("+")[0] != __version__:
            gaps.append(f"kit is stale: installed {installed}, running slopbrake {__version__}; run slopbrake init --update")
        stale = stale_files(repo, stack)
        if stale:
            gaps.append(f"kit is stale: {len(stale)} managed file(s) differ; run slopbrake init --update")
    bytecode = [f for f in git(repo, "ls-files", "--", "scripts/slopbrake", ".claude", check=False).splitlines()
                if f.endswith(".pyc")]
    if bytecode:
        gaps.append(f"kit bytecode is tracked in git ({len(bytecode)} .pyc file(s)); every gate run rewrites it: "
                    f"git rm --cached {' '.join(bytecode)}")
    stages = re.search(r"^STAGES=\(([^)]*)\)", read_text(repo / "scripts/check"), re.MULTILINE)
    if stack and stages:
        gaps += [f"scripts/check does not run the kit's {name} stage" for name in kit_stages(stack)
                 if name not in stages.group(1).split()]
    check_text = read_text(repo / "scripts/check")
    if TYPES_PLACEHOLDER in check_text:
        gaps.append("types stage is not configured: choose a type checker in scripts/check")
    if types_unconfigured(repo, stack, check_text):
        gaps.append('types stage skips: package.json has no "typecheck" script (add "typecheck": "tsc --noEmit")')
    if stack == "typescript" and not any("eslint-rules/slopbrake" in read_text(repo / name) for name in ESLINT_CONFIGS):
        gaps.append("eslint config does not load eslint-rules/slopbrake.mjs (T1 is not enforced)")
    if stack == "typescript" and check_text:
        gaps += ts_layout_gaps(repo, check_text)
    user_gaps, notes = user_hook_findings()
    gaps = list(dict.fromkeys(gaps + gate_gaps(check_text) + door_rule_gaps(repo) + user_gaps))
    return {"repo": str(repo), "files": present, "claude_md_lines": claude_lines, "hooks_path": hooks_path or None,
            "ci_runs_check": ci_runs_check, "stale_files": stale, "gaps": gaps, "notes": notes, "ok": not gaps}


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
TS_SEEDS = {  # {src} and {tests}: the repo's first source root and test dir (both top-level or packages/x/src)
    "boundary_impl": ("{src}/{pkg}/lib/slopbrake-probe.ts", "export const probe = 1;\n"),
    "boundary": ("{tests}/slopbrake-seed-boundary.test.ts",
                 ('import { expect, test } from "vitest";\nimport { probe } from "../{src}/{pkg}/lib/slopbrake-probe.js";\n\n'
                 'test("seeded deep import", () => {\n  expect(probe).toBeGreaterThan(0);\n});\n')),
    "tautology": ("{tests}/slopbrake-seed-tautology.test.ts",
                  ('import { expect, test } from "vitest";\n\ntest("seeded tautology", () => {\n'
                  "  const items = [10, 5];\n  expect(items.reduce((a, b) => a + b, 0)).toBe(items.reduce((a, b) => a + b, 0));\n"
                  "  expect(true).toBe(true);\n});\n")),
    "mutation_src": ("{src}/slopbrake-seed.ts",
                     ("export function discount(total: number, member: boolean): number {\n"
                     "  if (member && total >= 100) {\n    return total - 10;\n  }\n  return total;\n}\n")),
    "mutation_weak": ("{tests}/slopbrake-seed-mutation.test.ts",
                      ('import { expect, test } from "vitest";\nimport { discount } from "../{src}/slopbrake-seed.js";\n\n'
                      'test("discount runs", () => {\n  expect(discount(150, true)).toBeDefined();\n});\n')),
    "mutation_strong": ("{tests}/slopbrake-seed-mutation.test.ts",
                        ('import { expect, test } from "vitest";\nimport { discount } from "../{src}/slopbrake-seed.js";\n\n'
                        'test("members save ten from one hundred", () => {\n  expect(discount(150, true)).toBe(140);\n'
                        "  expect(discount(100, true)).toBe(90);\n  expect(discount(99, true)).toBe(99);\n"
                        "  expect(discount(150, false)).toBe(150);\n});\n")),
    "red": ("{tests}/slopbrake-seed-red.test.ts",
            ('import { expect, test } from "vitest";\nimport { discount } from "../{src}/slopbrake-seed.js";\n\n'
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
GIT_GUARD_BLOCK = ["git push --force origin main", "git push -f;echo done", 'bash -c "git reset --hard"', "git clean -fd",
                   "git branch -D old", "git checkout .", "git restore .", "git commit --no-verify -m x",
                   "git -c core.hooksPath=/dev/null commit -m x", "git push origin +main"]
GIT_GUARD_ALLOW = ["git push origin feature/x", "git status", "git restore --staged .",
                   'git commit -m "never run git reset --hard"', 'grep -rn "reset --hard" docs', "git push -u origin feature/x"]
GIT_GUARD_CASES = [(c, 2) for c in GIT_GUARD_BLOCK] + [(c, 0) for c in GIT_GUARD_ALLOW]


def kit_stages(stack: str) -> list[str]:
    """The STAGES the kit's scripts/check template runs: a full run must report every one."""
    match = re.search(r"^STAGES=\(([^)]*)\)", (KIT / stack / "scripts/check").read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1).split() if match else []


def missing_stages(output: str, required: list[str]) -> list[str]:
    """Required stages the run's summary does not report as pass or skip."""
    summary = output.rsplit("── summary ──", 1)[1] if "── summary ──" in output else ""
    seen = {m.group(2) for m in re.finditer(r"^(pass|skip)\s+(\S+)", summary, re.MULTILINE)}
    return [stage for stage in required if stage not in seen]


class Verifier:
    """Proofs run in self.wt, a detached worktree of HEAD (repo is a repository root, B8)."""

    def __init__(self, repo: Path, keep: bool):
        self.repo, self.keep = repo, keep
        self.head = git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False)
        if not self.head:
            raise UsageError(f"{repo} has no commits yet: commit the files `slopbrake init` wrote, then verify")
        self.proofs: list[dict] = []
        self.scratch: Path | None = None
        self.wt = repo
        self.stack = ""

    def setup(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="slopbrake-verify-"))
        self.wt = self.scratch / "wt"
        git(self.repo, "worktree", "add", "--detach", str(self.wt), self.head)
        try:
            self.stack = recorded_stack(self.wt) or detect_stack(self.wt)  # as committed
        except UsageError:
            return  # no kit at HEAD: verify reports it as a failed L1 proof
        if self.stack == "typescript" and (self.wt / "package.json").is_file():
            # A real install from the package manager's store; package managers reject a
            # symlinked node_modules and try to reinstall mid-check.
            install = {"pnpm": "pnpm install --frozen-lockfile --prefer-offline",
                       "yarn": "yarn install --frozen-lockfile --prefer-offline",
                       "bun": "bun install --frozen-lockfile", "npm": "npm ci --prefer-offline"}[package_manager(self.wt)]
            result = run(install, self.wt)
            if result.returncode != 0:
                raise UsageError(f"{install} failed in the verify worktree: {cause_line(result)}")

    def cleanup(self) -> None:
        if self.keep or self.scratch is None:
            return
        git(self.repo, "worktree", "remove", "--force", str(self.scratch / "wt"), check=False)
        git(self.repo, "worktree", "prune", check=False)
        shutil.rmtree(self.scratch, ignore_errors=True)

    def env(self, **extra: str) -> dict[str, str]:
        # Worktrees share refs: proofs must never move the last-green ratchet. Inherited PR context and
        # base pins would measure something other than the seeds; the proofs set their own (B15).
        inherited = {k: v for k, v in os.environ.items() if k not in VERIFY_SCRUB}
        return {**inherited, "SLOPBRAKE_BASE": self.head, "SLOPBRAKE_NO_RECORD": "1",
                "CLAUDE_PROJECT_DIR": str(self.wt), **extra}

    def check(self, *stages: str, **env: str) -> subprocess.CompletedProcess:
        return run(["scripts/check", *stages], self.wt, env=self.env(**env))

    def prove(self, name: str, expect_ok: bool, result: subprocess.CompletedProcess, rule: str,
              says: str | None = None, problems: list[str] | None = None) -> bool:
        """A result only counts when the check says so (`says`): a crash can't pass an expect-fail
        proof and a gate that runs nothing can't pass an expect-pass one."""
        text = result.stdout + result.stderr
        ok = (result.returncode == 0) == expect_ok and (says is None or says in text) and not problems
        lines = text.strip().splitlines()
        failed = [line for line in lines if line.startswith("FAIL  ")]  # the stage summary names what broke
        self.proofs.append({"proof": name, "rule": rule, "expected": ("pass" if expect_ok else "fail")
                            + (f" saying {says!r}" if says else ""), "exit": result.returncode, "ok": ok,
                            "output": [*(problems or []), *failed, *(line for line in lines[-6:] if line not in failed)]})
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
        sources, tests = ts_layout(self.wt)
        src = next((str(p.relative_to(self.wt)) for p in sorted(self.wt.glob(sources[0])) if p.is_dir()), "src")
        pkgs = sorted(p.name for p in (self.wt / src).iterdir() if p.is_dir()) if (self.wt / src).is_dir() else []
        fill = {"{src}": src, "{tests}": tests[0], "{pkg}": pkgs[0] if pkgs else "slopbrakepkg"}

        def filled(text: str) -> str:
            for key, value in fill.items():
                text = text.replace(key, value)
            return text
        return {k: (filled(p), filled(t)) for k, (p, t) in TS_SEEDS.items()}

    def run_all(self) -> None:
        seeds = self.seeds()
        # Clean tree: the full gate passes and runs every kit stage (mutation skips: nothing changed since HEAD).
        result, required = self.check(), kit_stages(self.stack)
        missing = missing_stages(result.stdout, required)
        problem = (f"stages missing from the summary as pass or skip: {' '.join(missing)} "
                   f"(the kit's STAGES: {' '.join(required)})")
        self.prove("clean tree passes scripts/check", True, result, "L1", problems=[problem] if missing else None)

        # D1: pass -> fail on a seeded deep import -> pass after reverting.
        added = [self.write(*seeds["boundary"])]
        if "boundary_impl" in seeds:
            added.append(self.write(*seeds["boundary_impl"]))
        self.prove("seeded deep import fails boundaries", False, self.check("boundaries"), "D1",
                   "tests-through-entrypoints" if self.stack == "python" else "entrypoint-boundary")
        for path in added:
            path.unlink()
        self.prove("boundaries pass again after revert", True, self.check("boundaries"), "D1", "pass  boundaries")

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
        self.prove("real assertions clear the mutation floor", True, self.check("mutation"), "T4",
                   "mutants killed" if self.stack == "python" else "Final mutation score")

        # Stop hook: red code blocks the stop (exit 2); a green tree lets it through.
        red = self.write(*seeds["red"])
        hook = self.wt / ".claude/hooks/require-green.sh"
        stop_input = json.dumps({"session_id": "verify", "stop_hook_active": False})
        result = run([str(hook)], self.wt, env=self.env(), stdin=stop_input)
        self.prove("Stop hook blocks on a red gate", False, result, "H6", "FAIL  tests")
        red.unlink()
        result = run([str(hook)], self.wt, env=self.env(), stdin=stop_input)
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

        # C8: a one-way change about to be committed on the default branch fails the pr stage. The worktree is
        # detached, so this runs in a clone with its own refs (the user's are untouched) whose only branch,
        # main, is the default by the kit's rule (no origin; init.defaultBranch=main).
        clone = self.scratch / "clone"
        run(["git", "clone", "-q", "--shared", "--no-checkout", str(self.repo), str(clone)], self.scratch, check=True)
        for args in (("remote", "remove", "origin"), ("config", "init.defaultBranch", "main"),
                     ("checkout", "-q", "-B", "main", self.head)):
            run(["git", *args], clone, check=True)
        (clone / "migrations").mkdir(exist_ok=True)
        (clone / "migrations/9998_slopbrake_seed.sql").write_text("DROP TABLE accounts;\n", encoding="utf-8")
        run(["git", "add", "migrations/9998_slopbrake_seed.sql"], clone, check=True)
        result = run(["scripts/check", "pr"], clone, env=self.env(CLAUDE_PROJECT_DIR=str(clone)))
        self.prove("a one-way change on the default branch blocks the commit", False, result, "G2",
                   "one-way door on main")

        # G1/G2/G4: PR body shape and the door floor, against a committed migration.
        self.write("migrations/9999_slopbrake_seed.sql", "DELETE FROM accounts;\n")
        run(["git", "add", "-A"], self.wt, check=True)
        run(["git", "-c", "user.name=slopbrake-verify", "-c", "user.email=verify@localhost", "commit", "-q",
             "--no-verify", "-m", "seed migration"], self.wt, check=True)
        body = self.scratch / "pr.md"
        checker = ["python3", "scripts/slopbrake/pr_body_check.py", "--body-file", str(body)]
        body.write_text(PR_BODY.format(door="one-way", rollback="Rollback: restore the accounts backup."), encoding="utf-8")
        self.prove("complete one-way PR body passes", True, run(checker, self.wt, env=self.env()), "G1/G4", "one-way")
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
            result = run([str(guard)], self.wt, stdin=json.dumps({"tool_input": {"command": command}, "cwd": str(self.wt)}))
            if result.returncode != expected:
                wrong.append(f"{command}: exit {result.returncode}, expected {expected}")
        self.prove("git guard blocks destructive commands only", True,
                   subprocess.CompletedProcess([], 1 if wrong else 0, "\n".join(wrong) or "all cases ok", ""), "H5")

        # H5/H6 only bite when Claude Code and git actually run the hooks.
        gaps = wiring_gaps(self.wt, local=self.repo)
        self.prove("Claude Code and git hooks are wired", True,
                   subprocess.CompletedProcess([], 1 if gaps else 0, "\n".join(gaps) or "wired", ""), "H5/H6")

        # H1: CLAUDE.md stays a short pointer file (as committed).
        lines = read_text(self.wt / "CLAUDE.md").splitlines()
        short = 0 < len(lines) <= CLAUDE_MD_MAX_LINES
        self.prove(f"CLAUDE.md is at most {CLAUDE_MD_MAX_LINES} lines", True,
                   subprocess.CompletedProcess([], 0 if short else 1, f"{len(lines)} lines", ""), "H1")


NO_KIT_AT_HEAD = ("HEAD has no slopbrake kit (scripts/check, .claude/slopbrake.json): run `slopbrake init` "
                  "if needed, commit the files `slopbrake init` wrote, then verify")


def _terminate(signum, _frame):
    raise SystemExit(128 + signum)  # unwinds through verify's finally, so the worktree is removed


def verify(repo: Path, keep: bool) -> dict:
    repo = repo_root(repo)  # a missing, non-git or subdirectory path is a usage error before anything runs
    if run(["git", "status", "--porcelain", "."], repo).stdout.strip():
        note = "working tree has uncommitted changes; verify proves the committed HEAD only"
    else:
        note = ""
    verifier = Verifier(repo, keep)
    previous = {sig: signal.signal(sig, _terminate) for sig in (signal.SIGTERM, signal.SIGHUP)}  # B12
    try:
        verifier.setup()
        if not verifier.stack or not (verifier.wt / "scripts/check").is_file():
            verifier.proofs.append({"proof": "Slopbrake is committed at HEAD", "rule": "L1", "ok": False,
                                    "output": [NO_KIT_AT_HEAD]})
        else:
            try:
                verifier.run_all()
            except Exception as exc:  # noqa: BLE001 - any harness error becomes a failed proof, not a traceback
                verifier.proofs.append({"proof": "verify harness", "ok": False, "output": [repr(exc)]})
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)  # a second TERM must not interrupt the cleanup
        verifier.cleanup()  # also when setup fails: no leaked worktree or scratch dir
        for sig, handler in previous.items():
            signal.signal(sig, handler)
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
            for note in r.get("notes", []):
                print(f"   note  {note}")
    elif command == "user-hooks":
        print("\n".join(harness.lines(result) if "harness" in result else hooks.user_lines(result)))
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
    p_user = sub.add_parser("user-hooks", help="wire the git guard and Stop gate into a harness's user-level hooks")
    p_user.add_argument("action", choices=["install", "uninstall", "status", "trust", "untrust"])
    p_user.add_argument("repo", type=Path, nargs="?", help="the repo to trust or untrust")
    p_user.add_argument("--harness", choices=harness.HARNESSES, default="claude",
                        help="claude (~/.claude/settings.json), codex (~/.codex/hooks.json) or opencode "
                             "(~/.config/opencode/plugins/slopbrake.js); trust and untrust apply to all")
    p_user.add_argument("--settings", type=Path, help="the harness's hooks file (default: as --harness says)")
    p_hook = sub.add_parser("hook", help="user-level Claude Code hook (reads the hook JSON on stdin)")
    p_hook.add_argument("event", nargs="?", help="pre-tool-use|post-tool-use|stop (hooks.main validates it: never exit 2)")
    for p in (p_init, p_status, p_verify, p_user):
        p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "hook":
        return hooks.main([args.event] if args.event else [])
    try:
        if args.command == "init":
            repo = repo_root(args.repo)
            stack = args.stack if args.stack != "auto" else recorded_stack(repo) or detect_stack(repo)
            result, ok = init(repo, stack, args.update, args.dry_run), True
        elif args.command == "status":
            repos = [repo_root(r) for r in args.repos]
            result = [status(r) for r in repos]
            ok = all(r["ok"] for r in result)
        elif args.command == "verify":
            result = verify(args.repo.resolve(), args.keep)
            ok = result["ok"]
        elif args.harness == "claude" or args.action in ("trust", "untrust"):
            result = hooks.user_command(args.action, args.settings, args.repo)
            ok = result["ok"]
        else:
            result = harness.user_command(args.harness, args.action, args.settings)
            ok = result["ok"]
    except (UsageError, hooks.SettingsError, RuntimeError) as exc:
        message = " ".join(str(exc).split())
        if args.json:
            print(json.dumps({"error": message}))
        print(f"slopbrake: {message}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_human(args.command, result)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
