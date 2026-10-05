#!/usr/bin/env python3
"""Print changed line ranges as path:start-end, comma-separated (for StrykerJS --mutate).

Only added lines since the merge-base with the base ref, including uncommitted and
untracked files; paths are from the repo root. Prints nothing when there are no matching changes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # a __pycache__ under scripts/slopbrake/ would itself be a one-way change
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import base_ref, collect_changes, glob_to_regex, repo_root


def ranges(lines: set[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for line in sorted(lines):
        if out and line == out[-1][1] + 1:
            out[-1] = (out[-1][0], line)
        else:
            out.append((line, line))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--include", action="append", default=[], help="glob of files to include (repeatable)")
    parser.add_argument("--exclude", action="append", default=[], help="glob of files to skip (repeatable)")
    args = parser.parse_args(argv)
    include = [glob_to_regex(g) for g in args.include] or [glob_to_regex("**")]
    exclude = [glob_to_regex(g) for g in args.exclude]
    base = base_ref(args.base)
    root = repo_root()
    specs = []
    for change in sorted(collect_changes(base, working_tree=True).values(), key=lambda c: c.path):
        if change.deleted or not change.added or not (root / change.path).is_file():
            continue
        if not any(rx.match(change.path) for rx in include) or any(rx.match(change.path) for rx in exclude):
            continue
        specs += [f"{change.path}:{start}-{end}" for start, end in ranges(set(change.added))]
    print(",".join(specs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
