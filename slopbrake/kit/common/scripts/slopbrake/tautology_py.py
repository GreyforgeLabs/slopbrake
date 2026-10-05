#!/usr/bin/env python3
"""Flag tautological Python test assertions and tests with no assertion (rule T1, the cheap mechanical cases).

A tautological test passes by construction: its expected value restates the code.
Flagged, when the expected side of an equality assertion:
  - is a literal and so is the actual side (``assert True``, ``assert 2 > 1``,
    ``assertEqual(1, 1)``, ``assertIn(1, [1])``),
  - is the same expression as the actual side, unless that expression calls the code
    under test with arguments or is compared by identity (``rng(seed=1) == rng(seed=1)``,
    ``cache.get("k") is cache.get("k")`` re-run the code: determinism and identity checks),
  - is computed with an aggregate (sum/len/reduce/map/sorted/...), directly or via a
    local variable assigned in the same test; ``len(f(xs)) == len(xs)`` applies the same
    aggregate to the result and is a property, not a restatement,
  - is derived from an implementation constant (``s[:MAX_LEN]``), or the actual side
    *is* that constant compared to a literal (``assert MAX_LEN == 280``),
  - recomputes the result from the same inputs passed to the code under test
    (``assert add(a, b) == a + b``). An f-string such as ``f"Hello, {name}!"`` is a spec
    expectation and is flagged only when it is the imported function's own return value.
``assertTrue(<comparison>)`` is judged like ``assert <comparison>``.
Suppress one assertion with a reason on the line or the line above:
    # slopbrake: allow-tautology: <why the expected value is independent>

A test (``test_*`` function, ``test*`` method of a test class) with no assertion only fails
by crashing. Assertions are ``assert``, ``raise AssertionError``, calls named ``assert*`` or
``expect*`` (not ``expected_*``), pytest raises/warns/fail and ``self.fail``, directly or in a
helper of the same file that asserts (one level; a test class inherits its in-file bases'
helpers). Skipped tests are ignored; conditionally skipped ones (skipif) are not, since they
run elsewhere. Suppress with a reason on the def line or above:
    # slopbrake: allow-no-assert: <why crashing is the only failure>
"""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # a __pycache__ under scripts/slopbrake/ would itself be a one-way change
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import python_files

EQUALITY_ASSERTS = {"assertEqual", "assertEquals", "assertIs", "assertListEqual", "assertDictEqual",
                    "assertTupleEqual", "assertSetEqual", "assertCountEqual", "assertAlmostEqual",
                    "assertSequenceEqual", "assertMultiLineEqual", "assert_equal"}
TRUTH_ASSERTS = {"assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone"}
ORDER_ASSERTS = {"assertIn", "assertNotIn", "assertNotEqual", "assertNotEquals", "assertIsNot", "assertGreater",
                 "assertGreaterEqual", "assertLess", "assertLessEqual", "assertNotAlmostEqual"}
PYTEST_ASSERTS = {"raises", "warns", "fail", "deprecated_call"}
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
AGGREGATES = {"sum", "len", "reduce", "map", "filter", "sorted", "max", "min", "fsum", "mean", "accumulate"}
BUILTINS = set(dir(builtins))
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", ".tox", "site-packages"}
ALLOW = re.compile(r"#\s*slopbrake:\s*allow-tautology:\s*\S")
ALLOW_NO_ASSERT = re.compile(r"#\s*slopbrake:\s*allow-no-assert:\s*\S")
ASSERT_CALL = re.compile(r"(assert|expect)($|[A-Z_])")  # assertEqual, expect_that; not expected_total
LITERALS = "compares a literal with a literal; nothing is under test"


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
        self.imports: dict[str, tuple[str, int, str]] = {}  # local name -> (module, level, name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.imports |= {a.asname or a.name: (node.module or "", node.level, a.name) for a in node.names}
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

    def allowed(self, lineno: int, pattern: re.Pattern[str] = ALLOW, *more: int) -> bool:
        return any(pattern.search(self.lines[i - 1]) for i in (lineno, lineno - 1, *more) if 0 < i <= len(self.lines))

    def implementation(self, call: ast.Call) -> ast.FunctionDef | None:
        """The definition of a function imported by name, when its module is in the repo."""
        if not isinstance(call.func, ast.Name) or call.func.id not in self.imports:
            return None
        module, level, name = self.imports[call.func.id]
        if not module:
            return None
        parts = module.split(".")
        here = list(self.path.resolve().parents)
        bases = here[level - 1:level] if level else [Path.cwd(), Path.cwd() / "src", *here]
        for base in bases:
            for path in (base.joinpath(*parts[:-1], parts[-1] + ".py"), base.joinpath(*parts, "__init__.py")):
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (OSError, SyntaxError, UnicodeDecodeError):
                    continue
                return next((f for f in tree.body if isinstance(f, FUNCS) and f.name == name), None)
        return None

    def restates_implementation(self, actual: ast.AST, expected: ast.AST) -> bool:
        """True when `expected` is a called function's own return expression, with the call's arguments bound."""
        for call in calls_code_under_test(actual):
            func = self.implementation(call)
            if func is None:
                continue
            bound = dict(zip([a.arg for a in [*func.args.posonlyargs, *func.args.args]], call.args))
            bound |= {k.arg: k.value for k in call.keywords if k.arg}
            if any(isinstance(ret, ast.Return) and ret.value is not None
                   and ast.dump(_Bind(bound).visit(copy.deepcopy(ret.value))) == ast.dump(expected)
                   for ret in ast.walk(func)):
                return True
        return False


class _Bind(ast.NodeTransformer):
    def __init__(self, bound: dict[str, ast.AST]):
        self.bound = bound

    def visit_Name(self, node: ast.Name) -> ast.AST:
        return self.bound.get(node.id, node)


def _is_test_module(module: str) -> bool:
    return any(part.startswith(("test", "fixture", "conftest")) for part in module.split("."))


def calls_code_under_test(node: ast.AST) -> list[ast.Call]:
    """Calls that are not aggregates or builtins (`len`, `int.from_bytes`)."""
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and call_name(n) not in AGGREGATES and call_name(n) not in BUILTINS
            and not (isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                     and n.func.value.id in BUILTINS)]


def aggregate_in(node: ast.AST) -> str | None:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and call_name(sub) in AGGREGATES and not all(is_literal(a) for a in sub.args):
            return call_name(sub)
    return None


def names_in(node: ast.AST) -> set[str]:
    """Value names read in `node`: not callees, builtins, or names a comprehension/lambda binds."""
    callees = {id(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}
    bound: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.comprehension):
            bound |= {t.id for t in ast.walk(n.target) if isinstance(t, ast.Name)}
        elif isinstance(n, ast.Lambda):
            bound |= {a.arg for a in [*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs]}
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and id(n) not in callees and n.id not in BUILTINS and n.id not in bound}


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


def judge(tf: TestFile, actual: ast.AST, expected: ast.AST, raw_pair, identity: bool = False) -> str | None:
    # Literal and self-comparison use the written pair: `before = f.read_bytes()` then
    # `f.read_bytes() == before` is a legitimate "unchanged" check, not a tautology.
    if is_literal(raw_pair[0]) and is_literal(raw_pair[1]):
        return LITERALS
    if ast.dump(raw_pair[0]) == ast.dump(raw_pair[1]):
        calls = calls_code_under_test(raw_pair[0])
        if calls and (identity or any(c.args or c.keywords for c in calls)):
            return None  # the code runs twice: an identity or determinism check
        return "compares an expression with itself"
    if is_literal(expected):
        const = tf.constant_in(actual)
        if const and not calls_code_under_test(actual):
            return f"asserts the value of implementation constant {const}; that restates its definition"
        return None
    const = tf.constant_operated_on(expected)
    if const:
        return f"expected value is derived from implementation constant {const}"
    if isinstance(actual, ast.Call) and call_name(actual) in AGGREGATES and calls_code_under_test(actual):
        return None  # `len(f(xs)) == len(xs)`: the same aggregate on both sides is a property
    inputs: set[str] = set()
    for call in calls_code_under_test(actual):
        for arg in [*call.args, *(k.value for k in call.keywords)]:
            inputs |= names_in(arg)
    used = names_in(expected)
    # Property checks built from the result (`frames == sorted(frames)`, or a second run:
    # `sort_all(xs) == sorted(sort_all(xs))`) are fine; an expected value rebuilt from the
    # *inputs* the code received is the tautology.
    result_names = names_in(raw_pair[0]) | names_in(raw_pair[1])
    tested = {ast.dump(c.func) for c in calls_code_under_test(actual)}
    reruns = any(ast.dump(c.func) in tested for c in calls_code_under_test(expected))
    from_inputs = bool(used & inputs) and not (used & result_names - inputs) and not reruns
    aggregate = aggregate_in(expected)
    if aggregate and from_inputs:
        return f"expected value is computed with {aggregate}() from the test inputs; use a literal or worked example"
    if from_inputs and used <= inputs and isinstance(expected, (ast.BinOp, ast.BoolOp)):
        return "expected value recomputes the result from the same inputs"
    if from_inputs and isinstance(expected, ast.JoinedStr) and tf.restates_implementation(actual, expected):
        return "expected value is the implementation's own f-string; write out the expected text"
    return None


def judge_test(tf: TestFile, test: ast.AST, assigns, lineno: int) -> str | None:
    """Judge the expression of `assert <test>` (or `assertTrue(<test>)`)."""
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test = test.operand
    if is_literal(test):
        return "asserts a literal; nothing is under test"
    if isinstance(test, ast.Compare):
        if all(is_literal(n) for n in (test.left, *test.comparators)):
            return LITERALS
        if len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.Is)):
            pair = (test.left, test.comparators[0])
            return judge(tf, *order(*pair, assigns, lineno), pair, isinstance(test.ops[0], ast.Is))
    return None


def body_nodes(func: ast.AST):
    """Nodes of a function body, not descending into nested functions or classes."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (*FUNCS, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def asserts(func: ast.AST, helpers: set[str] = frozenset(), methods: set[str] = frozenset()) -> bool:
    for node in body_nodes(func):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Raise) and isinstance(exc := getattr(node.exc, "func", node.exc), ast.Name) \
                and exc.id == "AssertionError":
            return True
        if not isinstance(node, ast.Call):
            continue
        name, owner = call_name(node), node.func.value if isinstance(node.func, ast.Attribute) else None
        if ASSERT_CALL.match(name):
            return True
        if owner is None and (name in helpers or name in PYTEST_ASSERTS):
            return True
        if isinstance(owner, ast.Name) and (owner.id == "self" and (name in methods or name == "fail")
                                            or owner.id == "pytest" and name in PYTEST_ASSERTS):
            return True
    return False


def skipped(node: ast.AST) -> bool:
    return any(call_name(d if isinstance(d, ast.Call) else ast.Call(func=d)) == "skip" for d in node.decorator_list)


def returns_value(func: ast.AST) -> bool:
    """A test-named function that returns a value is a helper (pytest rejects such tests)."""
    return any(isinstance(n, ast.Return) and n.value is not None
               and not (isinstance(n.value, ast.Constant) and n.value.value is None) for n in body_nodes(func))


def unasserted_tests(tree: ast.Module):
    """Tests with no assertion: module `test_*` functions and `test*` methods of test classes."""
    helpers = {f.name for f in tree.body if isinstance(f, FUNCS) and asserts(f)}
    inherited: dict[str, set[str]] = {}  # class name -> asserting methods, with bases from this file
    for node in tree.body:
        if isinstance(node, FUNCS) and (node.name == "test" or node.name.startswith("test_")):
            if not skipped(node) and not returns_value(node) and not asserts(node, helpers):
                yield node
        elif isinstance(node, ast.ClassDef):
            methods = [m for m in node.body if isinstance(m, FUNCS)]
            mine = {m.name for m in methods if asserts(m, helpers)}
            mine |= {name for b in node.bases if isinstance(b, ast.Name) for name in inherited.get(b.id, ())}
            inherited[node.name] = mine
            if (node.name.startswith("Test") or node.bases) and not skipped(node):
                    yield from (m for m in methods if m.name.startswith("test") and not skipped(m)
                            and not returns_value(m) and not asserts(m, helpers, mine))


def scan(path: Path) -> list[tuple[str, int, int, str]]:
    """Problems as (path, line, last line of the span it covers, message)."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, str(path))
    except SyntaxError as exc:
        return [(str(path), exc.lineno or 0, exc.lineno or 0, f"cannot parse: {exc.msg}")]
    tf = TestFile(path, tree, source.splitlines())
    problems = []
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for func in functions:
        assigns = local_assignments(func)
        for node in ast.walk(func):
            reason = None
            if isinstance(node, ast.Assert):
                reason = judge_test(tf, node.test, assigns, node.lineno)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
                name, args = node.func.attr, node.args
                if name in TRUTH_ASSERTS and is_literal(args[0]):
                    reason = f"{name}() on a literal; nothing is under test"
                elif name in ("assertTrue", "assertFalse"):
                    reason = judge_test(tf, args[0], assigns, node.lineno)
                elif name in EQUALITY_ASSERTS and len(args) >= 2:
                    reason = judge(tf, *order(args[0], args[1], assigns, node.lineno), args[:2], name == "assertIs")
                elif name in ORDER_ASSERTS and len(args) >= 2 and is_literal(args[0]) and is_literal(args[1]):
                    reason = LITERALS
            if reason and not tf.allowed(node.lineno):
                problems.append((str(path), node.lineno, node.lineno, f"tautological assertion: {reason}"))
    for func in unasserted_tests(tree):
        first = func.decorator_list[0].lineno - 1 if func.decorator_list else func.lineno
        if not tf.allowed(func.lineno, ALLOW_NO_ASSERT, first):
            problems.append((str(path), func.lineno, func.end_lineno or func.lineno,
                             f"no assertion: {func.name} asserts nothing, so it can only fail by crashing"))
    return sorted(set(problems))


def test_files(roots: list[Path]):
    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in python_files(root, SKIP_DIRS):
            if path.name.startswith("test") or path.name.endswith("_test.py"):  # unittest's test*.py, pytest's
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
        problems = [p for p in problems if added.get(str(Path(p[0]).resolve()), set()) & set(range(p[1], p[2] + 1))]
    for path, line, _, message in problems:
        print(f"{path}:{line}: {message}")
    tautologies = sum(m.startswith("tautological") for *_, m in problems)
    unasserted = sum(m.startswith("no assertion") for *_, m in problems)
    counts = [f"{n} {what}" for n, what in ((tautologies, "tautological assertion(s)"),
                                             (unasserted, "test(s) with no assertion")) if n]
    others = len(problems) - tautologies - unasserted
    counts += [f"{others} unparsable file(s)"] if others else []
    print(f"test-quality: {', '.join(counts)}" if problems else "test-quality: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
