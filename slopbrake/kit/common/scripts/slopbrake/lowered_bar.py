#!/usr/bin/env python3
"""Fail when a change quietly lowers the bar on the lines it touches (rule T6).

Measured since the base the other base-relative checks use (common.base_ref), over committed, uncommitted
and untracked changes. Fails when the change adds:
  - a skipped or focused test: Python's pytest.mark.skip/skipif/xfail, unittest.skip/skipIf/skipUnless/
    expectedFailure (also imported or through a module alias), self.skipTest(), pytest.skip()/xfail() (also
    imported bare), raise SkipTest, __test__ = False; JS/TS it/test/describe .skip, .skipIf, .todo, .fixme,
    xit/xtest/xdescribe, and .only/fit/fdescribe (which skip everything else), read in the test tree
    (test files, and files under tests/, test/, __tests__/, spec/, e2e/, cypress/);
  - a suppression comment: Python's noqa, type: ignore, pyright: ignore, pragma: no cover, pylint: disable;
    JS/TS eslint-disable (-line, -next-line, block), @ts-ignore, @ts-nocheck, @ts-expect-error and
    istanbul/c8/v8 ignore;
  - a deleted test: a test function (Python) or it()/test() case (JS/TS) that the base has and the test
    tree no longer has, matched by name over every test file the change touches. A test whose body moved
    to a new name (a rename) is not a deletion.
pytest.importorskip() is allowed: it skips only where an optional dependency is missing.

Only code and comments count: words inside strings, docstrings and regex literals are text. A marker the
same change removes elsewhere (an edited, re-indented or moved line) is not new. Lines that were already
there never fail, nor do lines a merge or rebase brought in from the default branch. With no shared history
to measure from (the base is the empty tree), only uncommitted changes count: committed lines passed this
stage at pre-commit. Vendored, fixture and generated files are not read: vendor/, third_party/, fixtures/,
__fixtures__/, testdata/, node_modules/ and the like, files marked linguist-vendored or linguist-generated
in .gitattributes, and files whose first 5 lines say @generated, DO NOT EDIT or auto-generated.

Say why instead, with a reason, on the line, the line above, or any line of the same bracketed span
(a wrapped decorator's closing parenthesis):
    # slopbrake: allow-skip: <why>        (or // in JS/TS)
    # slopbrake: allow-suppress: <why>
and for a deletion, on any added line of a file in the test tree, or in the PR body:
    slopbrake: allow-delete-test: <test name>: <why>
"""
from __future__ import annotations

import argparse
import ast
import bisect
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True  # a __pycache__ under scripts/slopbrake/ would itself be a one-way change
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (
    FileChange,
    base_ref,
    collect_changes,
    decode_python,
    default_branch,
    describe_base,
    empty_tree,
    git,
    merge_base,
    read_python,
    repo_root,
)

SKIP = 78
RULE = "T6"
PY = (".py", ".pyi")
JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")
JSX = (".jsx", ".tsx")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", ".tox", "site-packages",
             "vendor", "third_party", "third-party", "fixtures", "__fixtures__", "testdata"}
TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "e2e", "cypress"}
EXCLUDE = ["scripts/slopbrake/", ".claude/", "eslint-rules/"]  # the kit's own files, as mutation skips them
GENERATED = re.compile(r"@generated\b|\bDO NOT EDIT\b|\bauto-?generated\b", re.IGNORECASE)

JS_SKIP = re.compile(r"(?<![\w$.])(?:it|test|describe|suite|context|bench)(?:\s*\.\s*[A-Za-z_$][\w$]*)*?"
                     r"\s*\.\s*(skipIf|skip|todo|fixme|only)\b"
                     r"|(?<![\w$.])(xit|xtest|xdescribe|xcontext|fit|fdescribe|fcontext)\s*\(")
PY_SUPPRESS = re.compile(r"(?:(?:ruff|flake8)\s*:\s*)?noqa\b(?:\s*:\s*[A-Za-z]+\d+(?:\s*,\s*[A-Za-z]+\d+)*)?"
                         r"|type\s*:\s*ignore\b(?:\[[^\]]*\])?"
                         r"|pyright\s*:\s*ignore\b(?:\[[^\]]*\])?"
                         r"|pragma\s*:?\s*no\s*cover\b"
                         r"|pylint\s*:\s*disable(?:-next)?\s*=\s*[\w-]+(?:\s*,\s*[\w-]+)*", re.IGNORECASE)
JS_SUPPRESS = re.compile(r"(eslint-disable(?:-next-line|-line)?)\b([^\n]*)"
                         r"|@ts-(?:ignore|nocheck|expect-error)\b"
                         r"|(?:istanbul|c8|v8)\s+ignore\b(?!\s+stop\b)(?:\s+(?:next|if|else|file|start)\b(?:\s+\d+)?)?")
ALLOW = {"skip": re.compile(r"slopbrake:\s*allow-skip:\s*\S"), "suppress": re.compile(r"slopbrake:\s*allow-suppress:\s*\S")}
JS_TEST = re.compile(r"(?<![\w$.])(?:it|test)((?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*\(\s*")
REGEX_AFTER = set("(,=:[!&|?{};+-*%>~^")  # not `<`: in JSX, `</p>` closes a tag
REGEX_KEYWORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "yield",
                  "await", "instanceof"}
UNITTEST_SKIPS = ("skipIf", "skipUnless", "skip", "expectedFailure")
REMEDY = {
    "skip": "make the test pass or delete it in the open; if it must stay skipped, say why on the line: "
            "slopbrake: allow-skip: <reason>",
    "focus": "remove the focus before committing; if it is intended, say why on the line: slopbrake: allow-skip: <reason>",
    "suppress": "fix what the checker reports; if it is wrong here, say why on the line: "
                "slopbrake: allow-suppress: <reason>",
}


def lang(path: str | None) -> str | None:
    if not path:
        return None
    return "py" if path.endswith(PY) else "js" if path.endswith(JS) else None


def is_test_file(path: str | None) -> bool:
    """Python's test*.py / *_test.py (as test-quality reads them); JS/TS *.test.*, *.spec.*, Cypress *.cy.*,
    or under __tests__/."""
    if not path:
        return False
    name, parts = PurePosixPath(path).name, PurePosixPath(path).parts
    if lang(path) == "py":
        return name.startswith("test") or name.endswith("_test.py")
    if lang(path) == "js":
        return bool(re.search(r"\.(test|spec|cy)\.[^.]+$", name)) or "__tests__" in parts[:-1]
    return False


def in_test_tree(path: str) -> bool:
    return is_test_file(path) or any(part in TEST_DIRS for part in PurePosixPath(path).parts[:-1])


def generated(text: str) -> bool:
    return bool(GENERATED.search("\n".join(text.split("\n", 5)[:5])))


# ── lexing ───────────────────────────────────────────────────────────────────

@dataclass
class Lexed:
    code: str  # text with string, comment and regex contents blanked to spaces: offsets and lines hold
    comments: dict[int, list[str]]  # 1-based line -> comment bodies starting there (no delimiter)
    starts: list[int]

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    def code_lines(self) -> list[str]:
        return self.code.split("\n")


def lex(text: str, language: str, jsx: bool = False) -> Lexed:
    """Split source into code and comments without a parser, so a file that does not parse is still read.
    jsx: a template literal ends at the end of its line, since a backtick in JSX text (`<p>don`t</p>`) opens
    none; a multi-line template's later lines are then read as code."""
    out, comments, n, i = list(text), {}, len(text), 0
    starts = [0] + [k + 1 for k, c in enumerate(text) if c == "\n"]
    prev, prev_word, templates = "", "", []  # templates: brace depth of each open `${`

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    def comment(a: int, body: str) -> None:
        comments.setdefault(bisect.bisect_right(starts, a), []).append(body)

    def string_end(j: int, quote: str, multiline: bool) -> tuple[int, int]:
        """(where the contents end, index past the closing quote); unterminated, both are the end of the
        line (or of the text, for a triple-quoted string)."""
        while j < n:
            if text[j] == "\\":
                j += 2
            elif text.startswith(quote, j):
                return j, j + len(quote)
            elif text[j] == "\n" and not multiline:
                return j, j
            else:
                j += 1
        return n, n

    def template_end(j: int) -> tuple[int, int, bool]:
        """Template text from j: (where it ends, index past the closing backtick or `${`, whether it was `${`)."""
        while j < n:
            if text[j] == "\\":
                j += 2
            elif text[j] == "`":
                return j, j + 1, False
            elif text.startswith("${", j):
                return j, j + 2, True
            elif jsx and text[j] == "\n":
                return j, j, False
            else:
                j += 1
        return n, n, False

    while i < n:
        c = text[i]
        if (language == "py" and c == "#") or (language == "js" and text.startswith("//", i)):
            end = text.find("\n", i)
            end = n if end < 0 else end
            comment(i, text[i + (1 if c == "#" else 2):end])
            blank(i, end)
            i = end
        elif language == "js" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            comment(i, text[i + 2:end - 2 if text.endswith("*/", 0, end) else end])
            blank(i, end)
            i = end
        elif c in "'\"":
            quote = text[i:i + 3] if language == "py" and text[i:i + 3] in ('"""', "'''") else c
            contents, end = string_end(i + len(quote), quote, len(quote) == 3)
            blank(i + len(quote), contents)
            i, prev, prev_word = end, c, ""
        elif language == "js" and (c == "`" or c == "}" and templates and templates[-1] == 0):
            if c == "}":
                templates.pop()
            contents, end, opened = template_end(i + 1)
            blank(i + 1, contents)
            if opened:
                templates.append(0)
            i, prev, prev_word = end, "`", ""
        elif language == "js" and c == "/" and (not prev or prev in REGEX_AFTER or prev_word in REGEX_KEYWORDS):
            j, in_class = i + 1, False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            blank(i + 1, min(j, n))
            i, prev, prev_word = min(j + 1, n), "a", ""
        elif c.isalnum() or c in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            prev_word, prev = text[i:j], text[j - 1]
            i = j
        else:
            if language == "js" and templates and c in "{}":
                templates[-1] += 1 if c == "{" else -1
            if not c.isspace():
                prev, prev_word = c, ""
            i += 1
    return Lexed("".join(out), comments, starts)


# ── markers ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Marker:
    line: int
    kind: str     # skip, focus or suppress
    key: str      # what was added, normalised: a moved or reworded line carries the same key
    allowed: bool


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _alt(names) -> str:
    return "|".join(re.escape(name) for name in sorted(names, key=lambda x: (-len(x), x)))


def _imported(code: str, module: str, names: tuple[str, ...]) -> set[str]:
    """Local names bound by `from <module> import <name> [as <alias>]` for the given names."""
    found = set()
    for m in re.finditer(rf"^[ \t]*from[ \t]+{module}[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)", code, re.MULTILINE):
        for item in m.group(1).strip("()").split(","):
            parts = item.split()
            if parts and parts[0] in names:
                found.add(parts[2] if len(parts) >= 3 and parts[1] == "as" else parts[0])
    return found


def py_skip_pattern(code: str) -> re.Pattern[str]:
    """Python skip spellings, with the module aliases and bare names this file imports."""
    pytest = {"pytest"} | set(re.findall(r"^[ \t]*import[ \t]+pytest[ \t]+as[ \t]+(\w+)", code, re.MULTILINE))
    unittest = {"unittest"} | set(re.findall(r"^[ \t]*import[ \t]+unittest[ \t]+as[ \t]+(\w+)", code, re.MULTILINE))
    decorators = set(UNITTEST_SKIPS) | _imported(code, "unittest", UNITTEST_SKIPS)
    bare = _imported(code, "pytest", ("skip", "xfail"))
    parts = [rf"\b(?:(?:{_alt(pytest)})\s*\.\s*)?mark\s*\.\s*(?:skipif|skip|xfail)\b",
             rf"\b(?:{_alt(unittest)})\s*\.\s*(?:{_alt(UNITTEST_SKIPS)})\b",
             rf"^\s*@\s*(?:{_alt(decorators)})\b",
             rf"\b(?:{_alt(pytest)})\s*\.\s*(?:skip|xfail)\s*\(",
             r"\b\w+\s*\.\s*skipTest\s*\(",
             rf"\braise\s+(?:(?:{_alt(unittest)})\s*\.\s*)?SkipTest\b",
             r"^\s*__test__\s*=\s*False\b"]
    if bare:
        parts.append(rf"(?<![\w.])(?<!def\s)(?:{_alt(bare)})\s*\(")
    return re.compile("|".join(parts))


def span_end(code: list[str], line: int) -> int:
    """The last line of the bracketed span that starts on `line` (the line itself when it closes there)."""
    depth = 0
    for k in range(line - 1, min(len(code), line + 99)):
        depth += sum(ch in "([{" for ch in code[k]) - sum(ch in ")]}" for ch in code[k])
        if depth <= 0:
            return k + 1
    return line


def markers(lexed: Lexed, language: str, lines: set[int], test_tree: bool = True) -> list[Marker]:
    """Skips, focuses and suppressions on the given 1-based lines. JS/TS skips count only in the test tree,
    where `fit(` and `context.skip` mean a test; Python's spellings are specific enough to read anywhere."""
    code = lexed.code_lines()
    py_skip = py_skip_pattern(lexed.code) if language == "py" else None
    found = []

    def allowed(line: int, kind: str) -> bool:
        pattern = ALLOW["suppress" if kind == "suppress" else "skip"]
        return any(pattern.search(body) for at in range(line - 1, span_end(code, line) + 1)
                   for body in lexed.comments.get(at, ()))

    for line in sorted(lines):
        text = code[line - 1] if 0 < line <= len(code) else ""
        if py_skip is not None:
            hits = [("skip", re.sub(r"^raise(?=\S)", "raise ", re.sub(r"\s+|\($", "", m.group(0))))
                    for m in py_skip.finditer(text)]
        else:
            hits = []
            for m in JS_SKIP.finditer(text) if test_tree else ():
                key = re.sub(r"\s+|\($", "", m.group(0))
                hits.append(("focus" if (m.group(1) or "") == "only" or (m.group(2) or "").startswith("f") else "skip",
                             key))
        for body in lexed.comments.get(line, ()):
            hits += [("suppress", key) for key in suppressions(body, language)]
        found += [Marker(line, kind, key, allowed(line, kind)) for kind, key in hits]
    return found


def suppressions(body: str, language: str) -> list[str]:
    """Suppression directives a comment body starts with (Python: each `#` segment of the comment)."""
    if language == "py":
        return ["# " + _norm(m.group(0)) for seg in body.split("#") if (m := PY_SUPPRESS.match(seg.strip()))]
    m = JS_SUPPRESS.match(body.lstrip("/*! \t\n"))
    if not m:
        return []
    if m.group(1):  # the rules it disables, without the `-- description`
        return [_norm(f"{m.group(1)} {m.group(2).split('--')[0].removesuffix('*/')}")]
    return [_norm(m.group(0))]


# ── tests ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Test:
    name: str
    path: str
    line: int
    body: str | None  # fingerprint of what it does: a rename keeps it


def tests_in(text: str, path: str) -> list[Test]:
    if lang(path) == "py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [Test(m.group(1), path, text.count("\n", 0, m.start()) + 1, None)
                    for m in re.finditer(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test\w*)", text, re.MULTILINE)]
        return [Test(f.name, path, f.lineno, ast.dump(ast.Module(body=f.body, type_ignores=[])))
                for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                and f.name.startswith("test")]
    lexed = lex(text, "js", path.endswith(JSX))
    found = []
    for m in JS_TEST.finditer(lexed.code):
        start = m.end()
        if "todo" in m.group(1) or start >= len(text) or text[start] not in "'\"`":
            continue
        close = lexed.code.find(text[start], start + 1)
        if close < 0:
            continue
        depth, j, end = 0, m.start() + m.group(0).rindex("("), None
        while j < len(text):  # the call's closing paren, found on the blanked code
            depth += lexed.code[j] in "([{"
            depth -= lexed.code[j] in ")]}"
            if depth == 0:
                end = j
                break
            j += 1
        body = _norm(text[close + 1:end]) if end is not None else None
        found.append(Test(text[start + 1:close], path, lexed.line_of(start), body))
    return found


def deleted_tests(before: list[Test], after: list[Test]) -> list[Test]:
    """Tests in `before` with no same-named test in `after` and no renamed one (same body, new name)."""
    left = Counter(t.name for t in after)
    lost = []
    for test in before:
        if left[test.name]:
            left[test.name] -= 1
        else:
            lost.append(test)
    remaining = Counter(t.name for t in before)
    gained = Counter()
    for test in after:
        if remaining[test.name]:
            remaining[test.name] -= 1
        elif test.body is not None:
            gained[test.body] += 1
    out = []
    for test in lost:
        if test.body is not None and gained[test.body]:
            gained[test.body] -= 1
        else:
            out.append(test)
    return out


# ── reading ──────────────────────────────────────────────────────────────────

def blobs(fork: str, paths: set[str]) -> dict[str, bytes]:
    """Each path's content at `fork`, all in one `git cat-file --batch` (absent paths are left out)."""
    paths = sorted(p for p in paths if "\n" not in p)
    if fork == empty_tree() or not paths:
        return {}
    request = "".join(f"{fork}:{p}\n" for p in paths).encode("utf-8", "surrogateescape")
    out = subprocess.run(["git", "cat-file", "--batch"], input=request, cwd=repo_root(), capture_output=True,
                         check=False).stdout
    found, pos = {}, 0
    for path in paths:
        eol = out.find(b"\n", pos)
        if eol < 0:
            break
        header = out[pos:eol].split()
        pos = eol + 1
        if len(header) == 3 and header[2].isdigit():  # "<sha> <type> <size>", then the content and "\n"
            size = int(header[2])
            if header[1] == b"blob":
                found[path] = out[pos:pos + size]
            pos += size + 1
    return found


def decode(data: bytes, path: str) -> str:
    if lang(path) == "py":
        try:
            return decode_python(data)
        except (SyntaxError, UnicodeDecodeError):
            pass
    return data.decode("utf-8", "replace").removeprefix("﻿").replace("\r\n", "\n")


def read_now(root: Path, path: str) -> str | None:
    file = root / path
    if not file.is_file():
        return None
    if lang(path) == "py":
        try:
            return read_python(file)
        except (SyntaxError, UnicodeDecodeError):
            pass
    return decode(file.read_bytes(), path)


def linguist_excluded(paths: set[str]) -> set[str]:
    """Paths .gitattributes marks linguist-vendored or linguist-generated, in one `git check-attr` call."""
    paths = sorted(paths)
    if not paths:
        return set()
    result = subprocess.run(["git", "check-attr", "--stdin", "-z", "linguist-vendored", "linguist-generated"],
                            input="\0".join(paths).encode("utf-8", "surrogateescape"), cwd=repo_root(),
                            capture_output=True, check=False)
    fields = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    return {fields[k] for k in range(0, len(fields) - 2, 3) if fields[k + 2] in ("set", "true")}


def pr_body(body_file: str | None) -> str:
    from pr_body_check import read_body
    try:
        return read_body(body_file) or ""
    except (OSError, ValueError):
        return ""


# ── the check ────────────────────────────────────────────────────────────────

@dataclass
class Result:
    findings: list[tuple[str, int, str, str]] = field(default_factory=list)  # path, line, kind, key
    gone: list[Test] = field(default_factory=list)


class Scan:
    def __init__(self, root: Path, exclude: list[str]):
        self.root, self.exclude, self.linguist = root, exclude, set()

    def candidate(self, path: str | None) -> bool:
        return (lang(path) is not None and not any(path.startswith(p) for p in self.exclude)
                and not any(part in SKIP_DIRS for part in PurePosixPath(path).parts[:-1]))

    def wanted(self, path: str | None) -> bool:
        return self.candidate(path) and path not in self.linguist

    def run(self, fork: str, changes: list[FileChange]) -> Result:
        """Unallowed markers added since `fork` (not cancelled by a removal) and tests deleted since it."""
        self.linguist |= linguist_excluded({p for c in changes for p in (c.path, c.old_path) if self.candidate(p)})
        changes = [c for c in changes if self.wanted(c.path) or self.wanted(c.old_path)]
        olds = blobs(fork, {c.old_path or c.path for c in changes
                            if self.wanted(c.old_path or c.path) and (c.removed or is_test_file(c.old_path or c.path))})
        added: list[tuple[str, Marker]] = []
        removed: Counter[tuple[str, str]] = Counter()
        before: list[Test] = []
        after: list[Test] = []
        for change in sorted(changes, key=lambda c: c.path):
            old_path = change.old_path or change.path
            now = None if change.deleted or not self.wanted(change.path) else read_now(self.root, change.path)
            old = decode(olds[old_path], old_path) if old_path in olds else None
            now = None if now is not None and generated(now) else now
            old = None if old is not None and generated(old) else old
            if now is not None and change.added:
                language = lang(change.path)
                lexed = lex(now, language, change.path.endswith(JSX))
                added += [(change.path, m) for m in markers(lexed, language, set(change.added), in_test_tree(change.path))]
            if old is not None and change.removed:
                language = lang(old_path)
                lexed = lex(old, language, old_path.endswith(JSX))
                removed.update((m.kind, m.key) for m in markers(lexed, language, set(change.removed),
                                                                in_test_tree(old_path)))
            if old is not None and is_test_file(old_path):
                before += tests_in(old, old_path)
            if now is not None and is_test_file(change.path):
                after += tests_in(now, change.path)
        result = Result(gone=deleted_tests(before, after))
        for path, marker in added:
            if marker.allowed:
                continue
            if removed[(marker.kind, marker.key)]:  # the change removed this marker elsewhere: moved, not added
                removed[(marker.kind, marker.key)] -= 1
                continue
            result.findings.append((path, marker.line, marker.kind, marker.key))
        return result


def common_part(items: list, others: list, key) -> list:
    """The items of `items` that `others` also has (by key, each match used once)."""
    left = Counter(key(o) for o in others)
    kept = []
    for item in items:
        if left[key(item)]:
            left[key(item)] -= 1
            kept.append(item)
    return kept


def default_branch_forks(fork: str, head: str) -> list[str]:
    """Merge-bases of HEAD with the default branch (remote and local) that the change's own fork lacks: the
    default branch's commits merged or rebased in since `fork`. Lines they brought are not this change's."""
    name = default_branch()
    forks = []
    for ref in ([f"refs/remotes/origin/{name}", f"refs/heads/{name}"] if name else []):
        found = git("merge-base", ref, "HEAD", check=False).strip()
        if not found or found == head or found == fork or found in forks:
            continue
        if subprocess.run(["git", "merge-base", "--is-ancestor", found, fork], capture_output=True,
                          check=False).returncode != 0:
            forks.append(found)
    return forks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base")
    parser.add_argument("--exclude", action="append", default=list(EXCLUDE), help="path prefix to skip (repeatable)")
    parser.add_argument("--body-file", help="PR body, for allow-delete-test lines (default: as the pr stage finds it)")
    args = parser.parse_args(argv)

    base, described = base_ref(args.base), describe_base(args.base)
    if base is None:
        print(f"lowered-bar: skipped: {described}")
        return SKIP
    root = repo_root()
    head = git("rev-parse", "-q", "--verify", "HEAD^{commit}", check=False).strip()
    fork = merge_base(base) if head else empty_tree()
    if head and fork == empty_tree():
        # Measuring from nothing would re-judge every line ever committed, and keep the gate red for good.
        base = fork = head
        described = (f"HEAD: the base, {described}, shares no history with it, and committed lines passed this "
                     "stage at pre-commit, so only uncommitted changes count")
    changes = list(collect_changes(base, working_tree=True).values())
    scan = Scan(root, args.exclude)
    if not any(scan.candidate(c.path) or scan.candidate(c.old_path) for c in changes):
        print(f"lowered-bar: skipped: no changed Python or JavaScript/TypeScript files since {described}")
        return SKIP

    result = scan.run(fork, changes)
    for other in default_branch_forks(fork, head) if head else []:
        # Keep only what is also new since the default branch's merge-base: a merge or rebase from it
        # brings lines this change did not write.
        also = scan.run(other, list(collect_changes(other, working_tree=True).values()))
        result.findings = common_part(result.findings, also.findings, lambda f: f)
        result.gone = common_part(result.gone, also.gone, lambda t: t.name)

    allow_lines = [line for c in changes if in_test_tree(c.path) for line in c.added.values()]
    if result.gone:
        allow_lines.append(pr_body(args.body_file))
    problems: list[tuple[str, int, str]] = []
    counts: Counter[str] = Counter()
    for path, line, kind, key in result.findings:
        what = {"skip": "skipped test added", "focus": "focused test added", "suppress": "suppression added"}[kind]
        note = " (every other test in its scope stops running)" if kind == "focus" else ""
        problems.append((path, line, f"{RULE} {what}: {key}{note}; {REMEDY[kind]}"))
        counts[kind] += 1
    for test in result.gone:
        allow = re.compile(r"slopbrake:\s*allow-delete-test:\s*" + re.escape(test.name) + r"\s*:\s*\S")
        if any(allow.search(line) for line in allow_lines):
            continue
        message = (f"{RULE} test deleted: {test.name} (it is in the base and no test of that name, or with its body, "
                   "is left in the test tree); restore it, or record why on an added line in the test tree or in the "
                   f"PR body: slopbrake: allow-delete-test: {test.name}: <reason>")
        problems.append((test.path, test.line, message))
        counts["deleted"] += 1

    for path, line, message in sorted(problems):
        print(f"{path}:{line}: {message}")
    if problems:
        parts = [f"{counts[k]} {label}" for k, label in (("skip", "skipped test(s)"), ("focus", "focused test(s)"),
                                                         ("suppress", "suppression(s)"), ("deleted", "deleted test(s)"))
                 if counts[k]]
        print(f"lowered-bar: {', '.join(parts)} since {described}")
        return 1
    print(f"lowered-bar: ok (base {described})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
