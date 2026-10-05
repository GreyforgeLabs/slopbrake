#!/usr/bin/env python3
"""Enforce deep-module import boundaries for Python (rule D1).

Port of the dependency-cruiser rules in Pocock's setup-ts-deep-modules:
  entrypoint-boundary  outside a package, import only its entry points: the package
                       itself and the public modules at its root, never a subpackage
                       or an underscore-private module. Applies at every level: from
                       myapp/ui/view.py, myapp.billing is a package to enter through
                       its entry points like any top-level one.
  private-name         `from pkg_or_module import _name` from outside is a deep import.
  tests-through-entrypoints
                       tests obey the same rule against the top-level package, even
                       for the package they live in.
  no-circular          no import cycles between first-party modules (module-level
                       imports only; imports inside functions or under a positive
                       `if TYPE_CHECKING:` are the accepted way to defer a dependency).
Literal `importlib.import_module("x")` / `__import__("x")` calls count as imports. Folders
without __init__.py inside a package are namespace subpackages. First-party modules are
discovered at the repo root, under src/ (or src itself when it has an __init__.py), and
under packages/*/ and packages/*/src/ in monorepos.
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", ".tox",
             "site-packages", ".mypy_cache", ".pytest_cache", "runtime"}


def is_test_path(path: Path) -> bool:
    return any(p in ("tests", "test") for p in path.parts[:-1]) or path.name.startswith("test_") \
        or path.name.endswith("_test.py") or path.name == "conftest.py"


class Repo:
    def __init__(self, root: Path):
        self.root = root
        # dotted name -> file (packages map to __init__.py, namespace packages to their folder)
        self.modules: dict[str, Path] = {}
        monorepo = not (root / "packages/__init__.py").is_file()  # else packages/ is just a package
        projects = [root, *sorted(p for p in (root / "packages").glob("*") if p.is_dir() and monorepo)]
        for project in projects:
            src = project / "src"
            # src/__init__.py makes src itself the package; it is found in the project dir.
            for base, src_layout in ((project, False), (src, True)):
                if base.is_dir() and not (src_layout and (src / "__init__.py").is_file()):
                    self._scan_base(base, src_layout)
        self.tops = {name.split(".")[0] for name in self.modules}
        self._names = {file: name for name, file in self.modules.items()}

    def _scan_base(self, base: Path, src_layout: bool) -> None:
        for entry in sorted(base.iterdir()):
            if entry.name in SKIP_DIRS or entry.name.startswith(".") or entry.name in ("tests", "test"):
                continue
            if entry.is_file() and entry.suffix == ".py" and not is_test_path(entry) \
                    and entry.name not in ("setup.py", "conftest.py", "noxfile.py"):
                self.modules[entry.stem] = entry
            elif entry.is_dir() and ((entry / "__init__.py").is_file()
                                     or (src_layout and entry.name.isidentifier() and has_python(entry))):
                self._walk_package(entry, entry.name)  # under src/, a folder of modules is a namespace package

    def _walk_package(self, directory: Path, dotted: str) -> None:
        init = directory / "__init__.py"
        self.modules[dotted] = init if init.is_file() else directory
        for entry in sorted(directory.iterdir()):
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix == ".py" and entry.name != "__init__.py":
                self.modules[f"{dotted}.{entry.stem}"] = entry
            elif entry.is_dir() and entry.name.isidentifier() and has_python(entry):
                self._walk_package(entry, f"{dotted}.{entry.name}")

    def module_of(self, path: Path) -> str | None:
        return self._names.get(path)

    def is_package(self, dotted: str) -> bool:
        return dotted in self.modules and (self.modules[dotted].name == "__init__.py" or self.modules[dotted].is_dir())

    def resolve(self, dotted: str) -> str | None:
        """Longest first-party module prefix of a dotted import target."""
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            if ".".join(parts[:i]) in self.modules:
                return ".".join(parts[:i])
        return None


def has_python(directory: Path) -> bool:
    for _, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        if any(f.endswith(".py") for f in files):
            return True
    return False


def deferred_nodes(tree: ast.Module) -> set[int]:
    """Nodes that do not run at import time: function bodies and `if TYPE_CHECKING:` bodies."""
    deferred: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            deferred |= {id(n) for n in ast.walk(node) if n is not node}
        elif isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            negated = isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)
            deferred |= {id(n) for stmt in (node.orelse if negated else node.body) for n in ast.walk(stmt)}
    return deferred


def dynamic_target(node: ast.Call) -> str | None:
    """The literal module of `importlib.import_module("x")` / `import_module("x")` / `__import__("x")`."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    if name not in ("import_module", "__import__") or not node.args:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value and not arg.value.startswith("."):
        return arg.value
    return None


def imports_of(path: Path, tree: ast.Module, repo: Repo):
    """Yield (lineno, target, module_level) for absolute import targets, with names."""
    current = repo.module_of(path) or ""
    package = current if path.name == "__init__.py" else current.rpartition(".")[0]
    deferred = deferred_nodes(tree)
    for node in ast.walk(tree):
        top = id(node) not in deferred
        if isinstance(node, ast.Call) and (target := dynamic_target(node)):
            yield node.lineno, target, top
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, top
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".") if package else []
                anchor = anchor[:len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                base = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                base = node.module or ""
            for alias in node.names:
                target = f"{base}.{alias.name}" if base and alias.name != "*" else base
                yield node.lineno, target, top


def private_part(repo: Repo, target: str, entry: int = 0) -> str | None:
    """Why `target` reaches inside the package or module at parts[:entry + 1], or None if public."""
    parts = target.split(".")
    for i in range(entry + 1, len(parts)):
        segment, prefix = parts[i], ".".join(parts[: i + 1])
        if segment.startswith("_") and not segment.startswith("__"):
            return f"private name {prefix}"
        if repo.is_package(prefix):
            return f"subpackage {prefix}"
        if prefix not in repo.modules:
            return None  # a public attribute of an entry point
    return None


def entry_index(importer: list[str], target: list[str]) -> int | None:
    """Index of the entry `target` is reached through: its first segment past the longest prefix
    shared with the importer. None when the target is the importer or one of its ancestors."""
    i = 0
    while i < min(len(importer), len(target)) and importer[i] == target[i]:
        i += 1
    return i if i < len(target) else None


def violations(root: Path) -> list[tuple[str, str]]:
    """(message, baseline key) for every violation."""
    repo = Repo(root)
    problems: list[tuple[str, str]] = []
    graph: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if SKIP_DIRS & set(rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        testing = is_test_path(rel)
        me = repo.module_of(path)
        # Tests are judged from outside every package; code from where it sits.
        here = me.split(".") if me and not testing else []
        for lineno, target, top in imports_of(path, tree, repo):
            parts = target.split(".")
            if parts[0] not in repo.tops:
                continue
            entry = entry_index(here, parts)
            reason = None if entry is None else private_part(repo, target, entry)
            if reason:
                rule = "tests-through-entrypoints" if testing else "entrypoint-boundary"
                owner = ".".join(parts[: entry + 1])
                problems.append((f"{rel}:{lineno}: {rule}: imports {reason}; use {owner}'s entry points",
                                 f"{rel}: {rule}: {target}"))
            resolved = repo.resolve(target)
            if me and resolved and top and resolved != me:
                graph.setdefault(me, set()).add(resolved)
    for cycle in cycles(graph):
        message = f"no-circular: {' -> '.join(cycle + [cycle[0]])}"
        problems.append((message, message))
    return problems


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one module (Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component = []
            while True:
                item = stack.pop()
                on_stack.discard(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1:
                found.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return found


def legacy_baseline_key(problem: str) -> str:
    """The pre-0.2 key: the message without its line number (it dropped the full target)."""
    path, _, rest = problem.partition(":")
    line, _, detail = rest.partition(":")
    return f"{path}:{detail}" if line.isdigit() else problem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--baseline", help="accepted legacy violations; new ones still fail (a ratchet)")
    parser.add_argument("--write-baseline", action="store_true", help="record current violations as the baseline")
    args = parser.parse_args(argv)
    found = violations(Path(args.root).resolve())
    problems = [message for message, _ in found]
    if args.baseline:
        # Keys drop the line number, so unrelated edits don't churn the baseline, but keep the
        # full target, so a new deep import next to an accepted one still fails.
        baseline = Path(args.baseline)
        if args.write_baseline:
            baseline.write_text("".join(sorted({key + "\n" for _, key in found})), encoding="utf-8")
            print(f"boundaries: wrote {len(found)} accepted violation(s) to {baseline}")
            return 0
        accepted = set(baseline.read_text(encoding="utf-8").splitlines()) if baseline.is_file() else set()
        problems = [m for m, key in found if key not in accepted and legacy_baseline_key(m) not in accepted]
    for problem in problems:
        print(problem)
    print(f"boundaries: {len(problems)} violation(s)" if problems else "boundaries: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
