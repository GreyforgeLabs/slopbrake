#!/usr/bin/env python3
"""Flag tautological Python test assertions (rule T1, the cheap mechanical cases).

A tautological test passes by construction: its expected value restates the code.
Flagged, when the expected side of an equality assertion:
  - is a literal and so is the actual side (``assert True``, ``assertEqual(1, 1)``),
  - is the same expression as the actual side,
  - is computed with an aggregate (sum/len/reduce/map/sorted/...), directly or via a
    local variable assigned in the same test,
  - is derived from an implementation constant (``s[:MAX_LEN]``), or the actual side
    *is* that constant compared to a literal (``assert MAX_LEN == 280``),
  - recomputes the result from the same inputs passed to the code under test
    (``assert add(a, b) == a + b``).
Suppress one assertion with a reason on the line or the line above:
    # slopbrake: allow-tautology: <why the expected value is independent>
"""
from __future__ import annotations

import argparse
import ast
import builtins
import re
import sys
from pathlib import Path

EQUALITY_ASSERTS = {"assertEqual", "assertEquals", "assertIs", "assertListEqual", "assertDictEqual",
                    "assertTupleEqual", "assertSetEqual", "assertCountEqual", "assertAlmostEqual",
                    "assertSequenceEqual", "assertMultiLineEqual", "assert_equal"}
TRUTH_ASSERTS = {"assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone"}
AGGREGATES = {"sum", "len", "reduce", "map", "filter", "sorted", "max", "min", "fsum", "mean", "accumulate"}
BUILTINS = set(dir(builtins))
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", ".tox", "site-packages"}
ALLOW = re.compile(r"#\s*slopbrake:\s*allow-tautology:\s*\S")


def is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return is_literal(node.operand)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and is_literal(k) for k in node.keys) and all(is_literal(v) for v in node.values)
    return False


def call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def is_constant_name(name: str) -> bool:
    return name.isupper() and any(c.isalpha() for c in name)


class TestFile:
    def __init__(self, path: Path, tree: ast.Module, lines: list[str]):
        self.path, self.lines = path, lines
        self.constants: set[str] = set()  # imported UPPER names (bare)
        self.modules: set[str] = set()    # imported module aliases, for MODULE.CONST
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and not _is_test_module(node.module or ""):
                self.constants |= {a.asname or a.name for a in node.names if is_constant_name(a.asname or a.name)}
                self.modules |= {a.asname or a.name for a in node.names if not is_constant_name(a.asname or a.name)}
            elif isinstance(node, ast.Import):
                self.modules |= {(a.asname or a.name).split(".")[0] for a in node.names if not _is_test_module(a.name)}

    def constant_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name) and node.id in self.constants:
            return node.id
        if (isinstance(node, ast.Attribute) and is_constant_name(node.attr)
                and isinstance(node.value, ast.Name) and node.value.id in self.modules):
            return f"{node.value.id}.{node.attr}"
        return None

    def constant_in(self, node: ast.AST) -> str | None:
        return next((name for sub in ast.walk(node) if (name := self.constant_name(sub))), None)

    def constant_operated_on(self, node: ast.AST) -> str | None:
        """A constant used in arithmetic, indexing or a call; not a bare symbolic value."""
        for parent in ast.walk(node):
            if isinstance(parent, (ast.BinOp, ast.UnaryOp, ast.Subscript, ast.Slice, ast.Call)):
                for child in ast.iter_child_nodes(parent):
                    if name := self.constant_name(child):
                        return name
        return None

    def allowed(self, lineno: int) -> bool:
        return any(ALLOW.search(self.lines[i - 1]) for i in (lineno, lineno - 1) if 0 < i <= len(self.lines))


def _is_test_module(module: str) -> bool:
    return any(part.startswith(("test", "fixture", "conftest")) for part in module.split("."))


def calls_code_under_test(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and call_name(n) not in AGGREGATES and call_name(n) not in BUILTINS]


def aggregate_in(node: ast.AST) -> str | None:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and call_name(sub) in AGGREGATES and not all(is_literal(a) for a in sub.args):
            return call_name(sub)
    return None


def names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def local_assignments(func: ast.AST) -> list[tuple[int, str, ast.AST]]:
    found = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            found.append((node.lineno, node.targets[0].id, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            found.append((node.lineno, node.target.id, node.value))
    return sorted(found, key=lambda item: item[0])


def resolve(node: ast.AST, assigns, lineno: int) -> ast.AST:
    """Follow one level of `expected = <expr>` indirection inside the same test."""
    if isinstance(node, ast.Name):
        prior = [value for line, name, value in assigns if name == node.id and line < lineno]
        if prior:
            return prior[-1]
    return node


def order(first: ast.AST, second: ast.AST, assigns, lineno: int) -> tuple[ast.AST, ast.AST]:
    """Return (actual, expected). The side that calls the code under test is actual."""
    first_r, second_r = resolve(first, assigns, lineno), resolve(second, assigns, lineno)
    if calls_code_under_test(second_r) and not calls_code_under_test(first_r):
        return second_r, first_r
    return first_r, second_r


def judge(tf: TestFile, actual: ast.AST, expected: ast.AST, raw_pair) -> str | None:
    # Literal and self-comparison use the written pair: `before = f.read_bytes()` then
    # `f.read_bytes() == before` is a legitimate "unchanged" check, not a tautology.
    if is_literal(raw_pair[0]) and is_literal(raw_pair[1]):
        return "compares a literal with a literal; nothing is under test"
    if ast.dump(raw_pair[0]) == ast.dump(raw_pair[1]):
        return "compares an expression with itself"
    if is_literal(expected):
        const = tf.constant_in(actual)
        if const and not calls_code_under_test(actual):
            return f"asserts the value of implementation constant {const}; that restates its definition"
        return None
    const = tf.constant_operated_on(expected)
    if const:
        return f"expected value is derived from implementation constant {const}"
    inputs: set[str] = set()
    for call in calls_code_under_test(actual):
        for arg in [*call.args, *(k.value for k in call.keywords)]:
            inputs |= names_in(arg)
    used = names_in(expected)
    # Property checks built from the result (`frames == sorted(frames)`) are fine; an
    # expected value rebuilt from the *inputs* the code received is the tautology.
    result_names = names_in(raw_pair[0]) | names_in(raw_pair[1])
    from_inputs = bool(used & inputs) and not (used & result_names - inputs)
    aggregate = aggregate_in(expected)
    if aggregate and from_inputs:
        return f"expected value is computed with {aggregate}() from the test inputs; use a literal or worked example"
    if from_inputs and used <= inputs and isinstance(expected, (ast.BinOp, ast.JoinedStr, ast.BoolOp)):
        return "expected value recomputes the result from the same inputs"
    return None


def scan(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: cannot parse: {exc.msg}"]
    tf = TestFile(path, tree, source.splitlines())
    problems = []
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for func in functions:
        assigns = local_assignments(func)
        for node in ast.walk(func):
            reason = None
            if isinstance(node, ast.Assert):
                test = node.test
                if is_literal(test):
                    reason = "asserts a literal; nothing is under test"
                elif (isinstance(test, ast.Compare) and len(test.ops) == 1
                      and isinstance(test.ops[0], (ast.Eq, ast.Is))):
                    pair = (test.left, test.comparators[0])
                    reason = judge(tf, *order(*pair, assigns, node.lineno), pair)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                name = node.func.attr
                if name in TRUTH_ASSERTS and node.args and is_literal(node.args[0]):
                    reason = f"{name}() on a literal; nothing is under test"
                elif name in EQUALITY_ASSERTS and len(node.args) >= 2:
                    pair = (node.args[0], node.args[1])
                    reason = judge(tf, *order(*pair, assigns, node.lineno), pair)
            if reason and not tf.allowed(node.lineno):
                problems.append(f"{path}:{node.lineno}: tautological assertion: {reason}")
    return sorted(set(problems), key=lambda p: (p.split(":")[0], int(p.split(":")[1])))


def test_files(roots: list[Path]):
    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*.py")):
            if SKIP_DIRS & set(path.parts):
                continue
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                yield path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", default=["."], help="files or directories (default: .)")
    parser.add_argument("--changed", action="store_true",
                        help="report only assertions on lines added since the base (for legacy suites)")
    parser.add_argument("--base", help="base ref for --changed")
    args = parser.parse_args(argv)
    problems = [p for f in test_files([Path(p) for p in args.paths]) for p in scan(f)]
    if args.changed:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from common import base_ref, collect_changes, repo_root
        root = repo_root()
        added = {str((root / c.path).resolve()): set(c.added) for c in collect_changes(base_ref(args.base), True).values()}
        problems = [p for p in problems
                    if int(p.split(":")[1]) in added.get(str(Path(p.split(":")[0]).resolve()), set())]
    for problem in problems:
        print(problem)
    print(f"test-quality: {len(problems)} tautological assertion(s)" if problems else "test-quality: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
