#!/usr/bin/env python3
"""Check a PR body has the required shape (rules G1, G2, G4, T3).

Body source, first found: --body-file, $PR_BODY_FILE, the GitHub Actions pull_request
event. Outside a PR context the check is skipped (exit 0) unless --require is given.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import base_ref, collect_changes, git, repo_root
from door_classify import DEFAULT_RULES, classify, load_rules

SECTIONS = ("Summary", "Evidence", "Merge Danger", "Review log", "Open questions")
PLACEHOLDERS = ("<one-way or two-way>", "<one-word description>", "<diagram, diff-sketch, or tree>",
                "<screenshot/output/failing test run>", "<screenshot/output/passing test run>",
                "<finding>", "<only things a human must decide>")


def read_body(body_file: str | None) -> str | None:
    path = body_file or os.environ.get("PR_BODY_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8")
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and Path(event).is_file():
        pull = json.loads(Path(event).read_text(encoding="utf-8")).get("pull_request")
        if pull is not None:
            return pull.get("body") or ""
    return None


def strip_code(body: str) -> str:
    return re.sub(r"^(```|~~~).*?^\1\s*$", "", body, flags=re.DOTALL | re.MULTILINE)


def section(body: str, name: str) -> str:
    match = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", body, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else ""


def check(body: str, computed_door: str | None) -> list[str]:
    problems = []
    prose = strip_code(body)
    for name in SECTIONS:
        if not re.search(rf"^##\s+{re.escape(name)}\s*$", prose, re.MULTILINE | re.IGNORECASE):
            problems.append(f"missing section '## {name}' (G4)")
    doors = re.findall(r"\*\*Door:\*\*\s*(one-way|two-way)\b", prose, re.IGNORECASE)
    if len(doors) != 1:
        problems.append("Merge Danger needs exactly one '**Door:** one-way|two-way' (G1)")
    radius = re.search(r"\*\*Blast Radius:\*\*[ \t]*(\S.*)?", prose)
    if not radius or not radius.group(1):
        problems.append("Merge Danger needs '**Blast Radius:** <word>' with a value (G1)")
    for placeholder in PLACEHOLDERS:
        if placeholder in prose:
            problems.append(f"template placeholder left in body: {placeholder} (G4)")
    evidence = section(body, "Evidence")
    if not (re.search(r"\bbefore\b", evidence, re.IGNORECASE) and re.search(r"\bafter\b", evidence, re.IGNORECASE)):
        problems.append("Evidence must show a before and an after (T3/T5)")
    declared = doors[0].lower() if len(doors) == 1 else None
    if computed_door == "one-way" and declared == "two-way":
        problems.append("declared two-way, but door-classify computed one-way; a door may be raised, never lowered (G2)")
    if declared == "one-way" and not re.search(r"rollback|roll back|mitigation", section(body, "Merge Danger"), re.IGNORECASE):
        problems.append("one-way door needs a rollback or mitigation plan under Merge Danger (G3)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body-file")
    parser.add_argument("--base", help="base ref for the computed door floor")
    parser.add_argument("--require", action="store_true", help="fail when no PR body is found")
    parser.add_argument("--no-door-floor", action="store_true", help="skip comparing with door-classify")
    args = parser.parse_args(argv)

    body = read_body(args.body_file)
    if body is None:
        print("pr-body: no PR context; skipped")
        return 1 if args.require else 0
    computed = None
    if not args.no_door_floor:
        base = base_ref(args.base)
        rules_path = repo_root() / DEFAULT_RULES
        if base and rules_path.is_file():
            computed = classify(collect_changes(base, working_tree=False), load_rules(rules_path))["door"]
            body_path = str(Path(args.body_file).resolve()) if args.body_file else ""
            pending = [line[3:] for line in git("status", "--porcelain").splitlines()
                       if str((repo_root() / line[3:]).resolve()) != body_path]
            if pending:
                print(f"pr-body: note: {len(pending)} uncommitted change(s) are not in the door floor; "
                      "commit first, then check")
    problems = check(body, computed)
    for problem in problems:
        print(f"pr-body: {problem}")
    if not problems:
        print(f"pr-body: ok (computed door floor: {computed or 'n/a'})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
