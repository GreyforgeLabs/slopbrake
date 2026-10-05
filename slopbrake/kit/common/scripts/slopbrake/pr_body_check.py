#!/usr/bin/env python3
"""Check a PR body has the required shape (rules G1, G2, G3, G4, C3).

Body source, first found: --body-file, $PR_BODY_FILE, the GitHub Actions pull_request
event. On the default branch a staged (else uncommitted) one-way change fails, body or
not (C8: it belongs on a feature branch). Outside a PR context there is no body: the
check is otherwise skipped (exit 78), or fails with --require.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # a __pycache__ under scripts/slopbrake/ would itself be a one-way change
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import base_ref, default_branch, git, repo_root
from door_classify import (
    DEFAULT_RULES,
    RulesError,
    classify,
    effective_rules,
    measure,
    staged_changes,
    tracked_changes,
    working_changes,
)

SKIP = 78
SECTIONS = ("Summary", "Evidence", "Merge Danger", "Review log", "Open questions")
PLACEHOLDERS = ("<one-way or two-way>", "<one-word description>", "<diagram, diff-sketch, or tree>",
                "<screenshot/output/failing test run>", "<screenshot/output/passing test run>",
                "<finding>", "<only things a human must decide>")
ROLLBACK = re.compile(r"\b(?:roll[ -]?backs?|mitigations?)\b", re.IGNORECASE)
LABELS = re.compile(r"\*\*(?:Door|Blast Radius):\*\*[ \t]*[\w-]*", re.IGNORECASE)
# A plan names a concrete step in a clause with no negator: "restore the backup" counts;
# "none", "no way to restore", "restore is not possible" do not.
PLAN_STEP = re.compile(r"\b(?:revert|restor|back ?up|backed up|down[ -]?migration|downgrade|migrate down|snapshot|"
                       r"dump|flags?\b|disabl|toggl|undo|redeploy|re-?run|re-?apply|re-?enabl|re-?creat|replay|recover|"
                       r"reinstat|revers|rebuild|roll(?:ing)? forward|fix forward|kill[ -]?switch|canary|dual[ -]write|"
                       r"roll[ -]back|undeploy|make\s+rollback)",
                       re.IGNORECASE)
# "roll back" / "make rollback" / "undeploy" used as a step, not as a "Roll back:" label.
STEP_VERB = re.compile(r"\b(?:make\s+rollback|roll[ -]back|undeploy)\b(?!\s*(?:plan|strategy)?\s*(?::|\*\*))", re.IGNORECASE)
NEGATOR = re.compile(r"\b(?:no|not|none|nothing|nobody|never|without|cannot|impossible|irreversible|unable|"
                     r"(?:can|won|don|doesn|isn|aren|wasn|didn)['\u2019]t)\b|\bn/a\b", re.IGNORECASE)
CLAUSE = re.compile(r"[;,!?()]|\.(?=\s|$)")


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


def strip_hidden(body: str) -> str:
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)


def strip_code(body: str, inline: bool = True) -> str:
    """What a reader sees as prose: no HTML comments, code blocks (fenced, unclosed or indented) or inline code."""
    body = re.sub(r"^ {0,3}(```+|~~~+).*?(?:^ {0,3}\1\s*$|\Z)", "", strip_hidden(body), flags=re.DOTALL | re.MULTILINE)
    kept, after_blank, in_list, in_code = [], True, False, False
    for line in body.splitlines():
        if not line.strip():
            kept.append(line)
            after_blank = True
            continue
        indented = line.startswith(("    ", "\t"))
        in_code = indented and (in_code or (after_blank and not in_list))  # in a list it is a continuation
        if not in_code:
            kept.append(line)
            if not indented and (after_blank or re.match(r" {0,3}(?:[-*+]|\d+[.)])\s", line)):
                in_list = bool(re.match(r" {0,3}(?:[-*+]|\d+[.)])\s", line))
        after_blank = False
    body = "\n".join(kept)
    return re.sub(r"`+[^`\n]*`+", "", body) if inline else body


def section(body: str, name: str) -> str:
    match = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", body, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else ""


def names_a_step(text: str) -> bool:
    for clause in CLAUSE.split(text):
        step = PLAN_STEP.search(clause)
        if step and not NEGATOR.search(clause):
            return True
    return False


def has_rollback_plan(merge_danger: str) -> bool:
    """A rollback/mitigation label whose text (or the lines under a bare label) names a concrete step."""
    lines = [LABELS.sub(" ", line) for line in merge_danger.splitlines()]
    for i, line in enumerate(lines):
        verbs = [verb.span() for verb in STEP_VERB.finditer(line)]
        if verbs and names_a_step(line):
            return True
        labels = [label for label in ROLLBACK.finditer(line) if not any(a <= label.start() < b for a, b in verbs)]
        for n, label in enumerate(labels):
            if re.search(r"\b(?:no|not|without)\W*$", line[:label.start()], re.IGNORECASE):  # "no rollback"
                continue
            last = n + 1 == len(labels)
            text = line[label.end():len(line) if last else labels[n + 1].start()]
            if last and not re.sub(r"\b(?:plan|strategy)\b|\W", "", text, flags=re.IGNORECASE):
                text += " ; " + " ; ".join(itertools.takewhile(lambda nxt: not nxt.lstrip().startswith("**"), lines[i + 1:]))
            if names_a_step(text):
                return True
    return False


def doors(prose: str) -> list[str]:
    return [door.lower() for door in re.findall(r"\*\*Door:\*\*\s*(one-way|two-way)(?![\w-])", prose, re.IGNORECASE)]


def check(body: str, computed_door: str | None) -> list[str]:
    problems = []
    prose = strip_code(body)
    for name in SECTIONS:
        if not re.search(rf"^##\s+{re.escape(name)}\s*$", prose, re.MULTILINE | re.IGNORECASE):
            problems.append(f"missing section '## {name}' (G4)")
    found = doors(prose)
    if len(found) != 1:
        problems.append("Merge Danger needs exactly one '**Door:** one-way|two-way' (G1)")
    if not re.search(r"\*\*Blast Radius:\*\*[ \t]*[A-Za-z]", prose, re.IGNORECASE):
        problems.append("Merge Danger needs '**Blast Radius:** <word>' with a value (G1)")
    for placeholder in PLACEHOLDERS:
        if placeholder in prose:
            problems.append(f"template placeholder left in body: {placeholder} (G4)")
    evidence = section(prose, "Evidence")
    if not (re.search(r"\bbefore\b", evidence, re.IGNORECASE) and re.search(r"\bafter\b", evidence, re.IGNORECASE)):
        problems.append("Evidence must show a before and an after (T3/T5)")
    declared = found[0] if len(found) == 1 else None
    if computed_door == "one-way" and declared == "two-way":
        problems.append("declared two-way, but door-classify computed one-way; a door may be raised, never lowered (G2)")
    if declared == "one-way" and not has_rollback_plan(section(strip_code(body, inline=False), "Merge Danger")):
        problems.append("one-way door needs a rollback or mitigation plan under Merge Danger that names a step "
                        "(revert, restore, backup, down migration, flag, disable...); 'none' or 'n/a' is not a plan (G3)")
    return problems


def unlogged_reviews(body: str, merge_base: str) -> list[str]:
    """C3: `review:` commits since the base that the Review log does not mention (by finding or sha)."""
    log = section(strip_hidden(body), "Review log")
    flat = " ".join(log.lower().split())
    shas = re.findall(r"\b[0-9a-f]{7,40}\b", log.lower())
    missing = []
    for line in git("log", "--format=%H %s", f"{merge_base}..HEAD").splitlines():
        sha, _, subject = line.partition(" ")
        if not subject.lower().startswith("review:"):
            continue
        finding = " ".join(subject[len("review:"):].lower().split())
        logged = finding and re.search(rf"(?<!\w){re.escape(finding)}(?!\w)", flat)  # whole words: "on" is not "none"
        if not logged and not any(sha.startswith(s) for s in shas):
            missing.append(subject)
    return missing


def announce_one_way(reason: str) -> None:
    """Make a one-way PR visible in GitHub Actions (annotation + job summary)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    message = f"one-way door: a human reviews and merges this PR ({reason})"
    print(f"::warning::{message}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(f"- **{message}**\n")


def branch_name() -> str | None:
    return git("symbolic-ref", "-q", "--short", "HEAD", check=False).strip() or None


def no_pr_context(quiet: bool = False) -> int:
    """C8: block a one-way change about to be committed on the default branch.

    A hook's GIT_INDEX_FILE is exactly what the commit adds. scripts/check unsets it, so a non-empty index is
    judged together with tracked edits on disk (`commit -a` and `commit -- <path>` add them); an empty index
    means the whole uncommitted working tree. quiet: print only a block or an error.
    """
    say = (lambda _message: None) if quiet else print
    branch = branch_name()
    if branch is None or not git("rev-parse", "--verify", "-q", "HEAD", check=False).strip():
        say("pr: skipped: no PR context (not on a branch with commits)")
        return SKIP
    if branch != default_branch():
        say(f"pr: skipped: no PR context on {branch}")
        return SKIP
    views, what, empty = [staged_changes()], "staged change", "nothing staged"
    if not os.environ.get("GIT_INDEX_FILE"):  # a hook's own index is exactly the commit
        if views[0]:
            views.append(tracked_changes())
            what = "staged change (with tracked edits on disk)"
        else:
            views, what, empty = [working_changes("HEAD")], "uncommitted change", "nothing uncommitted"
    if not any(views):
        say(f"pr: skipped: no PR context and {empty} on {branch}")
        return SKIP
    try:
        rules = effective_rules("HEAD", repo_root() / DEFAULT_RULES)
        results = [classify(view, rules) for view in views]
    except RulesError as exc:
        print(f"pr: door rules: {exc}")
        return 1
    reasons = list(dict.fromkeys(r for result in results if result["door"] == "one-way" for r in result["reasons"]))
    if reasons:
        print(f"pr: one-way door on {branch}: commit it on a feature branch for operator review "
              "(a human may override with git commit --no-verify)")
        for reason in reasons:
            print(f"  - {reason}")
        return 1
    say(f"pr: skipped: no PR context; the {what} on {branch} is two-way")
    return SKIP


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body-file")
    parser.add_argument("--base", help="base ref for the computed door floor")
    parser.add_argument("--require", action="store_true", help="fail when no PR body is found")
    parser.add_argument("--no-door-floor", action="store_true",
                        help="skip the checks against the base (door floor, review traceability)")
    args = parser.parse_args(argv)

    body = read_body(args.body_file)
    if body is None:
        if args.require:
            print("pr-body: no PR context; a PR body is required")
            return 1
        if git("rev-parse", "--is-inside-work-tree", check=False).strip() != "true":
            print("pr: skipped: no PR context (not a git repository)")
            return SKIP
        return no_pr_context()
    if no_pr_context(quiet=True) == 1:  # B10: a body does not lift C8 on the default branch
        return 1
    problems, computed, floor = [], None, "not computed (--no-door-floor)"
    if not args.no_door_floor:
        base = base_ref(args.base)
        if base is None:
            problems.append("no base ref found, so the door floor cannot be computed; pass --base (G2)")
        else:
            try:
                result, warning = measure(base, working_tree=False)
                computed = result["door"]
                floor = f"{computed}, against {base}"
                if warning:
                    print(f"pr-body: warning: {warning}")
                if computed == "one-way":
                    announce_one_way(result["reasons"][0])
            except RulesError as exc:
                problems.append(f"door rules: {exc} (G2)")
            problems += [f"Review log is missing review: commit '{subject}' (C3)"
                         for subject in unlogged_reviews(body, git("merge-base", base, "HEAD").strip())]
            body_path = str(Path(args.body_file).resolve()) if args.body_file else ""
            pending = [line[3:] for line in git("status", "--porcelain").splitlines()
                       if str((repo_root() / line[3:]).resolve()) != body_path]
            if pending:
                print(f"pr-body: note: {len(pending)} uncommitted change(s) are not in the door floor; "
                      "commit first, then check")
    if computed != "one-way" and "one-way" in doors(strip_code(body)):
        announce_one_way("declared one-way in the PR body")
    problems += check(body, computed)
    for problem in problems:
        print(f"pr-body: {problem}")
    if not problems:
        print(f"pr-body: ok (computed door floor: {floor})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
