#!/usr/bin/env python3
"""Classify a change as a one-way or two-way door (rule G2).

The result is a floor: an agent may raise a two-way change to one-way, never lower it.
Rules live in .claude/door-rules.yml (path globs + regexes matched against added lines).
When a base exists, the base revision's rules count too, so a change cannot lower its own floor.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # a __pycache__ under scripts/slopbrake/ would itself be a one-way change
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (
    base_ref,
    collect_changes,
    git,
    glob_to_regex,
    load_simple_yaml,
    parse_unified_diff,
    repo_root,
    untracked_files,
)

DEFAULT_RULES = ".claude/door-rules.yml"
LISTS = ("one_way", "content_patterns", "ignore", "content_ignore")
KEYS = (*LISTS, "default")


class RulesError(Exception):
    pass


def parse_rules(text: str, source: str) -> dict[str, list[str]]:
    keys = [key.strip() for key in re.findall(r"^([^\s#-][^:\n]*)", text, re.MULTILINE)]
    for key in keys:
        if key not in KEYS:
            raise RulesError(f"{source}: unknown key {key!r}; allowed keys: {', '.join(KEYS)}")
        if keys.count(key) > 1:  # a second `one_way:` would silently replace the first list
            raise RulesError(f"{source}: duplicate key {key!r}; merge the lists under one '{key}:'")
    try:
        data = load_simple_yaml(text)
    except ValueError as exc:
        raise RulesError(f"{source}: {exc}") from None
    rules: dict[str, list[str]] = {}
    for key in LISTS:
        value = data.get(key, [])
        if not isinstance(value, list):  # list("x/**") would silently become one-character globs
            raise RulesError(f"{source}: {key} must be a list of '- item' lines, got {value!r}")
        rules[key] = value
    if data.get("default", "two_way") not in ("two_way", "one_way"):
        raise RulesError(f"{source}: default must be two_way or one_way")
    rules["default"] = [str(data.get("default", "two_way"))]
    return rules


def load_rules(path: Path) -> dict[str, list[str]]:
    return parse_rules(path.read_text(encoding="utf-8"), str(path))


def combine(base: dict | None, head: dict | None) -> dict[str, list[str]]:
    """Base rules are the floor: the change may add rules, never remove or exempt."""
    if base is None or head is None:
        rules = base or head
        if rules is None:
            raise RulesError(f"no door rules: {DEFAULT_RULES} is missing in the working tree and at the base")
        return rules
    def union(key):
        return base[key] + [item for item in head[key] if item not in base[key]]
    strict = "one_way" in (base["default"][0], head["default"][0])
    return {"one_way": union("one_way"), "content_patterns": union("content_patterns"),
            "ignore": base["ignore"], "content_ignore": base["content_ignore"],
            "default": ["one_way" if strict else "two_way"]}


def effective_rules(merge_base: str | None, head_path: Path) -> dict[str, list[str]]:
    head = load_rules(head_path) if head_path.is_file() else None
    base = None
    if merge_base:
        shown = subprocess.run(["git", "show", f"{merge_base}:{DEFAULT_RULES}"], cwd=repo_root(),
                               capture_output=True, text=True, check=False)
        if shown.returncode == 0:
            base = parse_rules(shown.stdout, f"{merge_base[:12]}:{DEFAULT_RULES}")
    return combine(base, head)


def working_changes(base: str) -> dict:
    """Committed, uncommitted and untracked changes, minus untracked Python bytecode caches."""
    changes = collect_changes(base, working_tree=True)
    for path in untracked_files():
        if "__pycache__" in path.split("/") or path.endswith(".pyc"):
            changes.pop(path, None)
    return changes


def _diff_head(*args: str) -> dict:
    return parse_unified_diff(git("diff", *args, "--no-color", "--no-ext-diff", "--no-textconv", "--text",
                                  "--unified=0", "--find-renames", "--src-prefix=a/", "--dst-prefix=b/", "HEAD",
                                  cwd=repo_root()))


def staged_changes() -> dict:
    """What the next commit adds: the index against HEAD (honours GIT_INDEX_FILE, e.g. `commit -a`)."""
    return _diff_head("--cached")


def tracked_changes() -> dict:
    """Tracked files on disk against HEAD: what `commit -a` or `commit -- <path>` may add beyond the index."""
    return _diff_head()


def measure(base: str, working_tree: bool, rules_path: Path | None = None):
    """Classify the change since `base`. Returns (result, warning or None)."""
    merge_base = git("merge-base", base, "HEAD").strip()
    rules = effective_rules(merge_base, rules_path or repo_root() / DEFAULT_RULES)
    changes = working_changes(base) if working_tree else collect_changes(base, working_tree=False)
    warning = None
    if not changes and merge_base == git("rev-parse", "HEAD").strip():
        warning = f"measuring nothing: HEAD is the base ({base})"
    return classify(changes, rules) | {"base": base}, warning


def classify(changes, rules) -> dict[str, object]:
    path_rules = [(glob, re.compile(glob_to_regex(glob).pattern, re.IGNORECASE)) for glob in rules["one_way"]]
    ignore = [glob_to_regex(glob) for glob in rules["ignore"]]
    content_ignore = [glob_to_regex(glob) for glob in rules["content_ignore"]]
    content = [(pattern, re.compile(pattern)) for pattern in rules["content_patterns"]]
    reasons: list[str] = []
    for change in changes.values():
        paths = {p for p in (change.path, change.old_path) if p}
        if all(any(rx.match(p) for rx in ignore) for p in paths):  # a file leaving docs/ is classified
            continue
        for glob, rx in path_rules:
            hit = next((p for p in sorted(paths) if rx.match(p)), None)
            if hit:
                verb = "deletes" if change.deleted else "touches"
                reasons.append(f"{verb} {hit} (path rule {glob!r})")
        if any(rx.match(change.path) for rx in content_ignore):
            continue
        for lineno, text in sorted(change.added.items()):
            if text.lstrip().startswith(("#", "//")):  # comment-only line
                continue
            for pattern, rx in content:
                if rx.search(text):
                    reasons.append(f"{change.path}:{lineno} adds {text.strip()[:80]!r} (content rule {pattern!r})")
    door = "one-way" if reasons or rules["default"][0] == "one_way" else "two-way"
    if not reasons and door == "one-way":
        reasons.append("repo default is one_way")
    return {"door": door, "reasons": reasons, "files": len(changes)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="base ref (default: SLOPBRAKE_BASE, CI base, main/master)")
    parser.add_argument("--rules", help=f"rules file (default: {DEFAULT_RULES}); the base's rules still apply")
    parser.add_argument("--diff-file", help="classify this unified diff instead of git")
    parser.add_argument("--working-tree", action="store_true", help="include uncommitted and untracked changes")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.diff_file:
            rules = load_rules(Path(args.rules) if args.rules else repo_root() / DEFAULT_RULES)
            changes = parse_unified_diff(Path(args.diff_file).read_text(encoding="utf-8"))
            result = classify(changes, rules) | {"base": None}
        else:
            base = base_ref(args.base)
            if base is None:
                print("door-classify: no base ref found; pass --base", file=sys.stderr)
                return 2
            result, warning = measure(base, args.working_tree, Path(args.rules) if args.rules else None)
            if warning:
                print(f"door-classify: warning: {warning}", file=sys.stderr)
    except (RulesError, OSError) as exc:
        print(f"door-classify: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"door: {result['door']}" + (f" (base {result['base']})" if result["base"] else ""))
        for reason in result["reasons"]:
            print(f"  - {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
