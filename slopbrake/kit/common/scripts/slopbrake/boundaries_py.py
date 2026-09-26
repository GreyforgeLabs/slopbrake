#!/usr/bin/env python3
"""Enforce deep-module import boundaries for Python (rule D1).

Port of the dependency-cruiser rules in Pocock's setup-ts-deep-modules:
  entrypoint-boundary  outside a package, import only its entry points: the package
                       itself and the public modules at its root, never a subpackage
                       or an underscore-private module.
  private-name         `from pkg_or_module import _name` from outside is a deep import.
  tests-through-entrypoints
                       tests obey the same rule, even for the package they live in.
  no-circular          no import cycles between first-party modules (module-level
                       imports only; imports inside functions or TYPE_CHECKING are
                       the accepted way to defer a dependency).
First-party modules are discovered at the repo root and under src/.
"""
from __future__ import annotations

import argparse
import ast
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
        self.modules: dict[str, Path] = {}  # dotted name -> file (packages map to __init__.py)
        for base in (root, root / "src"):
            if not base.is_dir():
                continue
            for entry in sorted(base.iterdir()):
                if entry.name in SKIP_DIRS or entry.name.startswith(".") or entry.name in ("tests", "test"):
                    continue
                if entry.is_file() and entry.suffix == ".py" and not is_test_path(entry) \
                        and entry.name not in ("setup.py", "conftest.py", "noxfile.py"):
                    self.modules[entry.stem] = entry
                elif entry.is_dir() and (entry / "__init__.py").is_file():
                    self._walk_package(entry, entry.name)
        self.tops = {name.split(".")[0] for name in self.modules}

    def _walk_package(self, directory: Path, dotted: str) -> None:
        self.modules[dotted] = directory / "__init__.py"
        for entry in sorted(directory.iterdir()):
            if entry.name in SKIP_DIRS:
                continue
            if entry.is_file() and entry.suffix == ".py" and entry.name != "__init__.py":
                self.modules[f"{dotted}.{entry.stem}"] = entry
            elif entry.is_dir() and (entry / "__init__.py").is_file():
                self._walk_package(entry, f"{dotted}.{entry.name}")

    def module_of(self, path: Path) -> str | None:
        return next((name for name, file in self.modules.items() if file == path), None)

    def package_of(self, path: Path) -> str | None:
        """The top-level first-party package a file sits inside, if any."""
        rel = path.relative_to(self.root).parts
        rel = rel[1:] if rel and rel[0] == "src" else rel
        return rel[0] if len(rel) > 1 and rel[0] in self.tops else None

    def is_package(self, dotted: str) -> bool:
        return dotted in self.modules and self.modules[dotted].name == "__init__.py"

    def resolve(self, dotted: str) -> str | None:
        """Longest first-party module prefix of a dotted import target."""
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            if ".".join(parts[:i]) in self.modules:
                return ".".join(parts[:i])
        return None


def imports_of(path: Path, tree: ast.Module, repo: Repo):
    """Yield (lineno, target, module_level) for absolute import targets, with names."""
    current = repo.module_of(path) or ""
    package = current if path.name == "__init__.py" else current.rpartition(".")[0]
    deferred = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or (
                isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test)):
            deferred |= {id(n) for n in ast.walk(node) if n is not node or isinstance(node, ast.If)}
    for node in ast.walk(tree):
        top = id(node) not in deferred
        if isinstance(node, ast.Import):
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


def private_part(repo: Repo, target: str) -> str | None:
    """Why `target` reaches inside a top-level package or module, or None if public."""
    parts = target.split(".")
    for i in range(1, len(parts)):
        segment, prefix = parts[i], ".".join(parts[: i + 1])
        if segment.startswith("_") and not segment.startswith("__"):
            return f"private name {prefix}"
        if repo.is_package(prefix):
            return f"subpackage {prefix}"
        if prefix not in repo.modules:
            return None  # a public attribute of an entry point
    return None


def check(root: Path) -> list[str]:
    repo = Repo(root)
    problems: list[str] = []
    graph: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if SKIP_DIRS & set(rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        importer_pkg = repo.package_of(path)
        testing = is_test_path(rel)
        me = repo.module_of(path)
        for lineno, target, top in imports_of(path, tree, repo):
            if target.split(".")[0] not in repo.tops:
                continue
            owner = target.split(".")[0]
            inside = importer_pkg == owner and not testing
            reason = None if inside else private_part(repo, target)
            if reason:
                rule = "tests-through-entrypoints" if testing else "entrypoint-boundary"
                problems.append(f"{rel}:{lineno}: {rule}: imports {reason}; use {owner}'s entry points")
            resolved = repo.resolve(target)
            if me and resolved and top and resolved != me:
                graph.setdefault(me, set()).add(resolved)
    problems += [f"no-circular: {' -> '.join(cycle + [cycle[0]])}" for cycle in cycles(graph)]
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


def baseline_key(problem: str) -> str:
    """A violation without its line number, so unrelated edits don't churn the baseline."""
    path, _, rest = problem.partition(":")
    line, _, detail = rest.partition(":")
    return f"{path}:{detail}" if line.isdigit() else problem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--baseline", help="accepted legacy violations; new ones still fail (a ratchet)")
    parser.add_argument("--write-baseline", action="store_true", help="record current violations as the baseline")
    args = parser.parse_args(argv)
    problems = check(Path(args.root).resolve())
    if args.baseline:
        baseline = Path(args.baseline)
        if args.write_baseline:
            baseline.write_text("".join(sorted({baseline_key(p) + "\n" for p in problems})), encoding="utf-8")
            print(f"boundaries: wrote {len(problems)} accepted violation(s) to {baseline}")
            return 0
        accepted = set(baseline.read_text(encoding="utf-8").splitlines()) if baseline.is_file() else set()
        problems = [p for p in problems if baseline_key(p) not in accepted]
    for problem in problems:
        print(problem)
    print(f"boundaries: {len(problems)} violation(s)" if problems else "boundaries: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
