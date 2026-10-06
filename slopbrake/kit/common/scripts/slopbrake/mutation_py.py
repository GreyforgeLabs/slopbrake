#!/usr/bin/env python3
"""Mutation-score floor on changed Python lines (rule T4: tests must be able to fail).

Mutates only source lines added since the base (tests excluded), in a scratch copy of
the tracked tree: the working tree is never modified, so an editable install that a
live service imports stays untouched. A mutant is killed when the test command fails
or times out. Below the floor the check fails and lists the surviving mutants; they
are evidence for the reviewer. Nothing to mutate exits 78 (a skip, not a pass).
When only tests changed, the functions and classes that the changed test functions call
(compared with the base, so a deleted assertion counts) are mutated in the first-party
modules those tests import, sampled to at most TESTS_ONLY_MAX mutants, so a weakened or
deleted test is still caught without mutating every module a test file happens to import.
A line ending in `# slopbrake: no-mutate` is never mutated (for equivalent mutants); every
changed line with mutation sites marked so fails the stage.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True  # a __pycache__ under scripts/slopbrake/ would itself be a one-way change
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (
    base_ref,
    collect_changes,
    describe_base,
    git,
    merge_base,
    repo_root,
    split_lines,
)

SKIP = 78
NO_MUTATE = re.compile(r"#\s*slopbrake:\s*no-mutate\s*$")
TESTS_ONLY_MAX = 40

COMPARE_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE,
                ast.LtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
BOUNDARY_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
BINOP_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.Div: ast.Mult,
              ast.FloorDiv: ast.Mult, ast.Mod: ast.FloorDiv, ast.Pow: ast.Mult}
SYMBOL = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.GtE: ">=", ast.Gt: ">", ast.LtE: "<=", ast.In: "in",
          ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not", ast.Add: "+", ast.Sub: "-", ast.Mult: "*",
          ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**", ast.And: "and", ast.Or: "or"}


def is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1]
    return any(p in ("tests", "test") for p in parts[:-1]) or name.startswith("test_") \
        or name.endswith("_test.py") or name == "conftest.py"


def unmutable_ids(tree: ast.AST) -> set[int]:
    """Constants a mutant must not touch: docstrings and bare strings, f-string text, string annotations,
    and strings that are lookups, arguments or names (keys, subscripts, call arguments, __all__,
    "__main__"): changing those mostly raises KeyError/LookupError, a crash "kill" no assertion earned,
    or only alters a message."""
    ids = set()
    for node in ast.walk(tree):
        strings = []
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            strings.append(node.value)
        elif isinstance(node, (ast.JoinedStr, getattr(ast, "TemplateStr", ast.JoinedStr))):
            strings += node.values
        elif isinstance(node, ast.Subscript):
            strings += node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        elif isinstance(node, ast.Call):
            strings += node.args + [k.value for k in node.keywords]
        elif isinstance(node, ast.Dict):
            strings += [k for k in node.keys if k is not None]
        elif isinstance(node, ast.Compare) and any(isinstance(n, ast.Name) and n.id == "__name__"
                                                   for n in [node.left, *node.comparators]):
            strings += node.comparators
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                strings += list(ast.walk(node.value))
        ids |= {id(n) for n in strings if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        annotations = [getattr(node, "annotation", None), getattr(node, "returns", None)]
        ids |= {id(n) for a in annotations if isinstance(a, ast.AST) for n in ast.walk(a)}
    return ids


def stringy(node: ast.AST) -> bool:
    """A str-valued expression, where `+ -> -` only crashes (a TypeError "kill" no test earned)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, bytes))
    if isinstance(node, ast.BinOp):
        return stringy(node.left) or stringy(node.right)
    if isinstance(node, ast.Call):
        func = node.func
        return (isinstance(func, ast.Name) and func.id in ("str", "repr", "format", "chr")) or (
            isinstance(func, ast.Attribute) and stringy(func.value))
    return isinstance(node, (ast.JoinedStr, getattr(ast, "TemplateStr", ast.JoinedStr)))


def sites(tree: ast.AST, lines: set[int]) -> list[tuple[ast.AST, str, int]]:
    """Deterministic list of (node, kind, detail) mutation sites on the given lines."""
    found = []
    skip = unmutable_ids(tree)
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) not in lines:
            continue
        if isinstance(node, ast.Compare):
            found += [(node, "compare", i) for i, op in enumerate(node.ops) if type(op) in COMPARE_SWAP]
            found += [(node, "boundary", i) for i, op in enumerate(node.ops) if type(op) in BOUNDARY_SWAP]
        elif isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in BINOP_SWAP:
            operands = (node.left, node.right) if isinstance(node, ast.BinOp) else (node.target, node.value)
            if not any(stringy(operand) for operand in operands):
                found.append((node, "binop", 0))
        elif isinstance(node, ast.BoolOp):
            found.append((node, "boolop", 0))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            found.append((node, "not", 0))
        elif isinstance(node, ast.Constant) and id(node) not in skip:
            if isinstance(node.value, int):  # bool included
                found.append((node, "constant", 0))
            elif isinstance(node.value, str):
                found.append((node, "string", 0))
        elif isinstance(node, ast.Expr) and isinstance(
                node.value.value if isinstance(node.value, ast.Await) else node.value, ast.Call):
            found.append((node, "call", 0))
        elif isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            found.append((node, "return", 0))
        elif isinstance(node, (ast.If, ast.While)) and not isinstance(node.test, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
            found.append((node, "condition", 0))
    return found


def apply(node: ast.AST, kind: str, detail: int) -> str:
    """Mutate node in place; return a short description."""
    if kind == "compare":
        old = type(node.ops[detail])
        node.ops[detail] = COMPARE_SWAP[old]()
        return f"{SYMBOL[old]} -> {SYMBOL[COMPARE_SWAP[old]]}"
    if kind == "boundary":
        old = type(node.ops[detail])
        node.ops[detail] = BOUNDARY_SWAP[old]()
        return f"{SYMBOL[old]} -> {SYMBOL[BOUNDARY_SWAP[old]]}"
    if kind == "binop":
        old = type(node.op)
        node.op = BINOP_SWAP[old]()
        return f"{SYMBOL[old]} -> {SYMBOL[BINOP_SWAP[old]]}"
    if kind == "boolop":
        old = type(node.op)
        node.op = ast.Or() if old is ast.And else ast.And()
        return f"{SYMBOL[old]} -> {'or' if old is ast.And else 'and'}"
    if kind == "not":
        node.operand = ast.UnaryOp(op=ast.Not(), operand=node.operand)
        return "drop not"
    if kind == "constant":
        old = node.value
        node.value = (not old) if isinstance(old, bool) else old + 1
        return f"{old!r} -> {node.value!r}"
    if kind == "string":
        old = node.value
        node.value = f"XX{old}XX"
        return f"string {repr(old)[:40]} -> 'XX..XX'"
    if kind == "call":
        node.value = ast.Constant(value=None)  # the statement stays, the call is gone
        return "delete call"
    if kind == "return":
        node.value = ast.Constant(value=None)
        return "return None"
    if kind == "condition":
        node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        return "negate condition"
    raise ValueError(kind)


def copy_tree(root: Path, dest: Path) -> None:
    listed = git("ls-files", "-co", "--exclude-standard", "-z", cwd=root)
    for rel in filter(None, listed.split("\0")):
        if "__pycache__" in rel.split("/"):  # a stale .pyc with the mutant's mtime and size would hide it
            continue
        src = root / rel
        if src.is_file() or src.is_symlink():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target, follow_symlinks=False)


def run_tests(cmd: str, cwd: Path, env: dict[str, str], timeout: float) -> tuple[bool, bool]:
    """(passed, timed_out). The command runs in its own process group and the whole group is
    killed afterwards: killing only the shell would orphan a looping test runner forever."""
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return proc.wait(timeout=timeout) == 0, False
    except subprocess.TimeoutExpired:
        return False, True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)  # unwinds through `finally`, so the scratch copy is removed


def imported_modules(root: Path, source: str) -> set[str]:
    """First-party modules (paths from the root) a test file's source imports, at the root or under src/."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names |= {node.module} | {f"{node.module}.{alias.name}" for alias in node.names}
    found = set()
    for name in names:
        for top in (root, root / "src"):
            for candidate in (top / (name.replace(".", "/") + ".py"), top / name.replace(".", "/") / "__init__.py"):
                if candidate.is_file():
                    found.add(str(candidate.relative_to(root)))
    return found


def changed_test_calls(old: str, new: str) -> set[str]:
    """Names called by test functions that were added, changed or removed between two versions of a file."""
    def functions(source: str) -> dict[str, str]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {}
        found = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = node.name if isinstance(node, ast.ClassDef) else ""
                    found[f"{owner}.{child.name}"] = child
        return found

    before, after = functions(old), functions(new)
    changed = [after[k] for k in after if k not in before or ast.dump(after[k]) != ast.dump(before[k])]
    changed += [before[k] for k in before if k not in after or ast.dump(after[k]) != ast.dump(before[k])]
    names = set()
    for function in changed:
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                func = node.func
                names.add(func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else "")
    return names - {""}


def defined_lines(path: Path, names: set[str]) -> dict[str, set[int]]:
    """Line spans of the module-level functions and classes (and their methods) named in `names`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    spans: dict[str, set[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            spans.setdefault(node.name, set()).update(range(node.lineno, node.end_lineno + 1))
    return spans


def mutable_lines(path: Path, lines: set[int]) -> set[int]:
    source = split_lines(path.read_text(encoding="utf-8"))
    return {n for n in lines if n <= len(source) and not NO_MUTATE.search(source[n - 1])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base")
    parser.add_argument("--floor", type=float, default=float(os.environ.get("MUTATION_FLOOR", "60")))
    parser.add_argument("--test-cmd", default=os.environ.get("MUTATION_TEST_CMD",
                                                               "python3 -m unittest discover -s tests"))
    parser.add_argument("--max-mutants", type=int, default=int(os.environ.get("MUTATION_MAX", "150")))
    parser.add_argument("--exclude", action="append", default=["scripts/slopbrake/", ".claude/", "eslint-rules/"],
                        help="path prefix never mutated (repeatable)")
    parser.add_argument("--json", help="also write the result as JSON to this path")
    args = parser.parse_args(argv)

    root = repo_root()
    base, described = base_ref(args.base), describe_base(args.base)
    if base is None:
        print(f"mutation: skipped: {described}")
        return SKIP
    changes = collect_changes(base, working_tree=True)

    def first_party(path: str) -> bool:
        return path.endswith(".py") and not any(path.startswith(prefix) for prefix in args.exclude)

    def wanted(path: str) -> bool:
        return first_party(path) and (root / path).is_file()

    live = [c for c in changes.values() if not c.deleted and wanted(c.path)]
    targets = {c.path: set(c.added) for c in live if c.added and not is_test_path(c.path)}
    marked = {path: lines - mutable_lines(root / path, lines) for path, lines in targets.items()}
    pragmas = sum(len(lines) for lines in marked.values())
    # Visible to the reviewer in every outcome: each excluded line claims its mutants are equivalent.
    print(f"mutation: {pragmas} changed line{'s' if pragmas != 1 else ''} excluded by no-mutate")
    tests_only = not targets
    if tests_only:  # weakened (even deleted) assertions change only tests: mutate what the changed tests call
        fork = merge_base(base)

        def base_source(change) -> str:
            return git("show", f"{fork}:{change.old_path or change.path}", check=False)

        def head_source(change) -> str:
            return "" if change.deleted else (root / change.path).read_text(encoding="utf-8", errors="replace")

        tests = [c for c in changes.values()
                 if is_test_path(c.path) and (wanted(c.path) or c.deleted and first_party(c.path))]
        modules = {m for c in tests for m in imported_modules(root, head_source(c) or base_source(c))
                   if wanted(m) and not is_test_path(m)}
        if not modules:
            print(f"mutation: skipped: no changed Python source lines since {described}")
            return SKIP
        called = set().union(*(changed_test_calls(base_source(c), head_source(c)) for c in tests))
        spans = {m: defined_lines(root / m, called) for m in sorted(modules)}
        spans = {m: found for m, found in spans.items() if found}
        if not spans:
            print(f"mutation: skipped: only tests changed since {described}, and the changed tests "
                  "call no first-party function")
            return SKIP
        print(f"mutation: only tests changed since {described}; mutating what the changed tests call: "
              + "; ".join(f"{m} ({', '.join(sorted(found))})" for m, found in spans.items()))
        targets = {m: set().union(*found.values()) for m, found in spans.items()}
        marked = {path: lines - mutable_lines(root / path, lines) for path, lines in targets.items()}
        args.max_mutants = min(args.max_mutants, TESTS_ONLY_MAX)
    targets = {path: lines - marked[path] for path, lines in targets.items()}
    plan, hidden = [], 0
    for path, lines in sorted(targets.items()):
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"mutation: cannot parse {path}: {exc}")
            return 1
        plan += [(path, index) for index in range(len(sites(tree, lines)))]
        hidden += len(sites(tree, marked[path]))
    if not plan and hidden and not tests_only:
        print(f"mutation: every changed line is marked no-mutate; mark only equivalent mutants (base {described})")
        return 1
    if not plan:
        print(f"mutation: skipped: no mutable sites on the changed lines since {described}")
        return SKIP
    if len(plan) > args.max_mutants:
        step = len(plan) / args.max_mutants
        plan = [plan[int(i * step)] for i in range(args.max_mutants)]

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _terminate)
    scratch = Path(tempfile.mkdtemp(prefix="slopbrake-mutation-"))
    try:
        work = scratch / "repo"
        copy_tree(root, work)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=os.pathsep.join(filter(None, [str(work), str(work / "src"), os.environ.get("PYTHONPATH")])))
        started = time.monotonic()
        passed, _ = run_tests(args.test_cmd, work, env, timeout=600)
        if not passed:
            print(f"mutation: the test command fails before mutation ({args.test_cmd}); fix the tests first")
            return 1
        timeout = max(20.0, (time.monotonic() - started) * 4 + 5)

        killed, survivors = 0, []
        for path, index in plan:
            original = (work / path).read_text(encoding="utf-8")
            tree = ast.parse(original)
            node, kind, detail = sites(tree, targets[path])[index]
            line = node.lineno
            description = apply(node, kind, detail)
            (work / path).write_text(ast.unparse(ast.fix_missing_locations(tree)) + "\n", encoding="utf-8")
            try:
                ok, _timed_out = run_tests(args.test_cmd, work, env, timeout)
            finally:
                (work / path).write_text(original, encoding="utf-8")
            if ok:
                source = split_lines(original)[line - 1].strip()
                survivors.append({"path": path, "line": line, "mutation": description, "source": source})
            else:
                killed += 1
        total = len(plan)
        score = 100.0 * killed / total
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    for s in survivors:
        print(f"survived: {s['path']}:{s['line']} [{s['mutation']}] {s['source'][:90]}")
    verdict = "ok" if score >= args.floor else "below floor"
    print(f"mutation: {killed}/{total} mutants killed = {score:.0f}% (floor {args.floor:.0f}%) {verdict}; "
          f"base {described}; test command: {args.test_cmd}")
    if args.json:
        Path(args.json).write_text(json.dumps({"score": score, "floor": args.floor, "killed": killed, "total": total,
                                               "survivors": survivors, "base": base,
                                               "no_mutate_lines": pragmas}, indent=2), encoding="utf-8")
    return 0 if score >= args.floor else 1


if __name__ == "__main__":
    sys.exit(main())
