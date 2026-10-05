"""Shared helpers for Slopbrake's checks: git base discovery, diff parsing, globs.

Standard library only, so every check runs in CI without an install step.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    # quotePath off: non-ASCII names come out verbatim, not as "\303\251" octal escapes.
    # errors="replace": `diff --text` prints binary files' raw bytes.
    result = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, capture_output=True,
                            encoding="utf-8", errors="replace", check=False)
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def repo_root() -> Path:
    return Path(git("rev-parse", "--show-toplevel").strip())


def _ref_exists(ref: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "-q", ref + "^{commit}"],
                          capture_output=True, check=False).returncode == 0


def _is_ancestor(ref: str, of: str = "HEAD") -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", ref, of], capture_output=True, check=False).returncode == 0


@functools.cache
def empty_tree() -> str:
    """The empty tree's id: a base before every commit, so every line is new."""
    return git("hash-object", "-t", "tree", os.devnull).strip()


def merge_base(base: str) -> str:
    """merge-base(base, HEAD); the empty tree when base is the empty tree or shares no history with HEAD."""
    found = "" if base == empty_tree() else git("merge-base", base, "HEAD", check=False).strip()
    return found or empty_tree()


def last_green_ref(branch: str) -> str:
    """The branch's ratchet ref, "/" encoded as %2F so feature and feature/x never collide.
    run-stages.sh and require-green.sh spell the same rule in bash."""
    return "refs/slopbrake/last-green/" + branch.replace("/", "%2F")


def _branch() -> str:
    return git("symbolic-ref", "-q", "--short", "HEAD", check=False).strip()


def last_green() -> str | None:
    """This branch's ratchet (HEAD after its last full green run, recorded by scripts/check),
    when it exists and is an ancestor of HEAD. Never on a detached HEAD."""
    branch = _branch()
    ref = last_green_ref(branch)
    return ref if branch and _ref_exists(ref) and _is_ancestor(ref) else None


def _since_last_green(branch: str) -> tuple[str, str]:
    """Base for commits that no remote has: everything since the branch's last full green run."""
    ref = last_green_ref(branch)
    if branch and _ref_exists(ref):
        if _is_ancestor(ref):
            return ref, f"HEAD is on {branch}"
        return merge_base(ref), "ratchet not an ancestor"  # amended, rebased or reset past it
    return empty_tree(), "no ratchet"


def _select_base(explicit: str | None) -> tuple[str | None, str]:
    """(ref, how it was chosen). See base_ref."""
    given = [(explicit, "--base"), (os.environ.get("GITHUB_BASE_REF") and "origin/" + os.environ["GITHUB_BASE_REF"],
                                    "GITHUB_BASE_REF"), (os.environ.get("SLOPBRAKE_BASE"), "SLOPBRAKE_BASE")]
    base, how = next(((ref, how) for ref, how in given if ref), (None, ""))
    head = git("rev-parse", "-q", "--verify", "HEAD", check=False).strip()
    if base:
        # An explicit base at HEAD itself means "uncommitted work only"; keep it. So does the empty tree.
        if base == empty_tree() or git("rev-parse", "-q", "--verify", base + "^{commit}", check=False).strip() == head:
            return base, how
    else:
        upstream = git("rev-parse", "-q", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False).strip()
        auto = ([upstream] if upstream else []) + ["origin/HEAD", "main", "master", "origin/main", "origin/master"]
        base = next((ref for ref in auto if _ref_exists(ref)), None)
        how = "upstream" if base and base == upstream else "default"
        if base is None:  # no base ref: this branch is the only line of history
            return _since_last_green(_branch()) if head else (None, "")
        # The base is HEAD's own local branch (no remote has its commits): since the last green run, never
        # "nothing committed". A remote-tracking base at HEAD already has (and gated) every commit.
        branch = _branch()
        if head and branch and git("rev-parse", "--symbolic-full-name", base, check=False).strip() == "refs/heads/" + branch:
            return _since_last_green(branch)
    # HEAD is on the base: the base shows nothing committed, so measure since the last green run.
    if head and git("merge-base", base, "HEAD", check=False).strip() == head and (green := last_green()):
        return green, f"HEAD is on {base}"
    return base, how


def base_ref(explicit: str | None = None) -> str | None:
    """The ref (or tree) a change is measured against.

    --base flag > GITHUB_BASE_REF (as origin/<x>) > SLOPBRAKE_BASE > the branch's upstream > origin/HEAD > main >
    master > origin/main > origin/master. When HEAD is on that base, this branch's last green run instead
    (see last_green). On the base branch itself with no remote holding its commits, or with no base ref at
    all: the ratchet; merge-base(ratchet, HEAD) when it is no longer an ancestor; the empty tree when there
    is none, so commits are never skipped. Callers needing a merge-base use merge_base(): it may be a tree.
    """
    return _select_base(explicit)[0]


def describe_base(explicit: str | None = None) -> str:
    """base_ref for humans: which ref, at which commit, and why."""
    base, how = _select_base(explicit)
    if base is None:
        return "no base ref (no commits yet)"
    branch = _branch()
    if how == "no ratchet":
        return f"the empty tree (no last-green ratchet for {branch or 'this detached HEAD'} yet, so every line is new)"
    sha = git("rev-parse", "--short", base + "^{commit}", check=False).strip() or "?"
    if how == "ratchet not an ancestor":
        return (f"{sha}, the merge-base with last-green ({last_green_ref(branch)} is not an ancestor of HEAD: "
                "amended, rebased or reset)")
    if base.startswith("refs/slopbrake/last-green/"):
        return f"last-green {sha} (last full green run on {branch}; {how})"
    if base == empty_tree():
        return f"the empty tree (from {how})"
    return f"{base} {sha}" + (f" (from {how})" if how not in ("", "default") else "")


@dataclass
class FileChange:
    path: str
    old_path: str | None = None
    deleted: bool = False
    added: dict[int, str] = field(default_factory=dict)  # new line number -> text


_PATH = r'("(?:[^"\\]|\\.)*"|[ab]/.*?)'
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13}


def unquote_path(text: str) -> str:
    """Git's C-style quoted path ("a/caf\\303\\251.py") back to the name, without the a/ or b/ prefix."""
    if text.startswith('"'):
        raw, i, body = bytearray(), 0, text[1:-1]
        while i < len(body):
            char = body[i]
            if char != "\\":
                raw += char.encode()
                i += 1
            elif body[i + 1:i + 4].isdigit() and len(body[i + 1:i + 4]) == 3:
                raw.append(int(body[i + 1:i + 4], 8))
                i += 4
            else:
                raw.append(_ESCAPES.get(body[i + 1], ord(body[i + 1])))
                i += 2
        text = raw.decode("utf-8", "surrogateescape")
    return text[2:] if text[:2] in ("a/", "b/") else text


def parse_unified_diff(text: str) -> dict[str, FileChange]:
    """Parse `git diff --unified=0` output into per-file added lines."""
    changes: dict[str, FileChange] = {}
    current: FileChange | None = None
    new_line = 0
    in_hunk = False  # `+++ b/path` is a header only before the first hunk; inside one it is content
    # Only "\n" ends a diff line: str.splitlines() also splits on \f, \v, \x1c.. inside content.
    for line in text.split("\n"):
        line = line.removesuffix("\r")
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            half = (len(rest) - 1) // 2
            if rest[:2] == "a/" and rest[half:half + 3] == " b/" and rest[2:half] == rest[half + 3:]:
                old = new = rest[2:half]  # unquoted and unrenamed: the halves match, even with " b/" inside
            else:
                match = re.match(_PATH + " " + _PATH.replace(".*?", ".*") + "$", rest)
                old, new = (unquote_path(match.group(1)), unquote_path(match.group(2))) if match else (None, None)
            current = FileChange(path=new or "", old_path=old)
            changes[current.path] = current
            in_hunk = False
        elif current is None:
            continue
        elif line.startswith("+++ ") and not in_hunk and line[4:] != "/dev/null":
            path = unquote_path(line[4:].removesuffix("\t"))  # unambiguous, where the header is not
            if path != current.path:
                changes.pop(current.path, None)
                current.path = path
                changes[path] = current
        elif line.startswith("deleted file mode") and not in_hunk:
            current.deleted = True
        elif line.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            new_line = int(match.group(1)) if match else 0
            in_hunk = True
        elif line.startswith("+") and in_hunk:
            current.added[new_line] = line[1:]
            new_line += 1
    return changes


def split_lines(text: str) -> list[str]:
    """Lines as git and ast number them: only "\n" ends a line (splitlines() also splits on \f, \v, \x1c..)."""
    lines = text.split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def untracked_files() -> list[str]:
    """Untracked, not-ignored files, as paths from the repo root (whatever the cwd)."""
    out = git("ls-files", "--others", "--exclude-standard", "-z", cwd=repo_root())
    return [p for p in out.split("\0") if p]


def python_files(root: Path, skip: set[str]) -> list[Path]:
    """Sorted *.py files under root. Never enters a `skip` name, .claude/ (kit state and Claude Code's
    worktrees) or a nested checkout: a directory holding a .git file (a linked worktree, a submodule)."""
    found = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip and d != ".claude" and not os.path.isfile(os.path.join(dirpath, d, ".git"))]
        found += [Path(dirpath, f) for f in files if f.endswith(".py")]
    return sorted(found)


def collect_changes(base: str | None, working_tree: bool) -> dict[str, FileChange]:
    """Changes since the merge-base with `base`.

    working_tree=False: committed changes only (merge-base...HEAD), the PR view.
    working_tree=True: also uncommitted and untracked files, the local-gate view.
    No common history (an orphan branch, no commits yet) or the empty tree as base: everything counts as new.
    """
    if base is None:
        return {}
    if base != empty_tree() and not _ref_exists(base):
        raise SystemExit(f"base ref {base!r} does not exist (fetch it, or set SLOPBRAKE_BASE)")
    has_head = bool(git("rev-parse", "-q", "--verify", "HEAD", check=False).strip())
    if not has_head and not working_tree:
        return {}
    since = merge_base(base) if has_head else empty_tree()
    # --text/--no-textconv: `-diff` attributes and binary-looking files must not hide added lines.
    args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "--unified=0", "--find-renames",
            "--src-prefix=a/", "--dst-prefix=b/", since]
    if not working_tree:
        args.append("HEAD")
    changes = parse_unified_diff(git(*args, cwd=repo_root()))
    if working_tree:
        root = repo_root()
        for path in untracked_files():
            try:
                lines = split_lines((root / path).read_text(encoding="utf-8"))
            except (UnicodeDecodeError, OSError):
                lines = []
            changes[path] = FileChange(path=path, added={i + 1: t for i, t in enumerate(lines)})
    return changes


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Gitignore-style glob: `**/` spans zero or more directories, `*` stays in one."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out += "(?:/.*)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


def load_simple_yaml(text: str) -> dict[str, object]:
    """Parse the YAML subset the rule files use: `key: scalar` and `key:` + `- item` lists.

    Kept dependency-free so CI needs no PyYAML. Double-quoted scalars use JSON escapes.
    """
    data: dict[str, object] = {}
    key: str | None = None
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t", "-")):
            name, _, value = line.partition(":")
            key = name.strip()
            data[key] = [] if value.strip() in ("", "[]") else _scalar(value.strip())
        elif line.strip().startswith("- ") and key is not None:
            items = data.setdefault(key, [])
            if not isinstance(items, list):
                raise ValueError(f"key {key!r} mixes a scalar and a list")
            items.append(_scalar(line.strip()[2:].strip()))
        else:
            raise ValueError(f"unsupported YAML line: {raw!r}")
    return data


def _strip_comment(line: str) -> str:
    quote, i = None, 0
    while i < len(line):
        char = line[i]
        if quote == '"' and char == "\\":
            i += 2  # skip the escaped character
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
        i += 1
    return line


def _scalar(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value
