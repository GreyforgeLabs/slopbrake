"""Behaviour tests for the Python boundary check (D1), through its command line."""
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

GUARDS = Path(__file__).resolve().parents[1] / "slopbrake/kit/common/scripts/slopbrake"
sys.path.insert(0, str(GUARDS))

from boundaries_py import Repo


def boundaries(root, *args):
    return subprocess.run([sys.executable, str(GUARDS / "boundaries_py.py"), ".", *args], cwd=root,
                          capture_output=True, text=True, check=False)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel, text=""):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def violations(self, *args):
        result = boundaries(self.root, *args)
        self.assertEqual(result.returncode, 1 if result.stdout.splitlines()[:-1] else 0, result.stdout + result.stderr)
        return result.stdout.splitlines()[:-1]


class IntraPackage(Scratch):
    def setUp(self):
        super().setUp()
        for rel in ("__init__", "billing/__init__", "billing/internal/__init__", "ui/__init__"):
            self.write(f"src/myapp/{rel}.py")
        self.write("src/myapp/billing/__init__.py", "from ._ledger import post\nfrom .internal import deep\n")
        self.write("src/myapp/billing/_ledger.py", "def post():\n    pass\n")
        self.write("src/myapp/billing/internal/deep.py", "from .._ledger import post\nSECRET = 1\n")
        self.write("src/myapp/billing/api.py", "from ._ledger import post\nfrom .internal.deep import SECRET\n")

    def test_sibling_subpackages_are_entered_through_their_entry_points(self):
        self.write("src/myapp/ui/view.py", "from myapp.billing._ledger import post\n"
                   "from myapp.billing.internal.deep import SECRET\nfrom ..billing.internal import deep\n"
                   "from myapp.billing import post as p\nfrom myapp.billing.api import post as q\nfrom . import widgets\n")
        self.write("src/myapp/ui/widgets.py", "def _helper():\n    pass\n")
        self.write("src/myapp/ui/menu.py", "from .widgets import _helper\n")
        self.assertEqual(self.violations(), [
            "src/myapp/ui/menu.py:1: entrypoint-boundary: imports private name myapp.ui.widgets._helper; use myapp.ui.widgets's entry points",
            "src/myapp/ui/view.py:1: entrypoint-boundary: imports private name myapp.billing._ledger; use myapp.billing's entry points",
            "src/myapp/ui/view.py:2: entrypoint-boundary: imports subpackage myapp.billing.internal; use myapp.billing's entry points",
            "src/myapp/ui/view.py:3: entrypoint-boundary: imports subpackage myapp.billing.internal; use myapp.billing's entry points",
        ])

    def test_tests_go_through_the_top_level_entry_points(self):
        self.write("tests/test_billing.py", "from myapp import VERSION\n")
        self.write("tests/test_deep.py", "from myapp.billing.api import post\n")
        self.write("tests/test_probe.py", "from myapp._slopbrake_probe import value\n")
        self.assertEqual(self.violations(), [
            "tests/test_deep.py:1: tests-through-entrypoints: imports subpackage myapp.billing; use myapp's entry points",
            "tests/test_probe.py:1: tests-through-entrypoints: imports private name myapp._slopbrake_probe; use myapp's entry points",
        ])


class FlatLayout(Scratch):
    def test_flat_single_module_repo(self):
        self.write("gf_skills.py", "def load():\n    pass\n")
        self.write("scripts/slopbrake/common.py", "")
        self.write("scripts/slopbrake/door.py", "from common import x\n")
        self.write("tests/test_sync.py", "from gf_skills import load\n")
        self.assertEqual(self.violations(), [])
        self.assertEqual(Repo(self.root).tops, {"gf_skills"})
        self.write("tests/test_probe.py", "from gf_skills._slopbrake_probe import value\n")
        self.assertEqual(self.violations(), [
            "tests/test_probe.py:1: tests-through-entrypoints: imports private name gf_skills._slopbrake_probe; use gf_skills's entry points",
        ])


class DynamicAndNamespace(Scratch):
    def test_literal_dynamic_imports_and_namespace_folders_are_checked(self):
        self.write("src/app/__init__.py")
        self.write("src/app/core/__init__.py")
        self.write("src/app/core/engine.py", "def go():\n    pass\n")
        self.write("src/app/core/_secret.py", "K = 1\n")
        self.write("src/app/plugins/loader.py", "def load():\n    pass\n")
        self.write("src/app/plugins/deep/x.py", "")
        self.write("src/app/runner.py", "import importlib\nimportlib.import_module('app.plugins.loader')\n")
        self.write("src/nsapp/core/engine.py", "def go():\n    pass\n")
        self.write("src/other/c05.py", "import importlib\nimportlib.import_module('app.core.engine')\n")
        self.write("src/other/c06.py", "__import__('app.core._secret', fromlist=['k'])\n")
        self.write("src/other/c07.py", "from importlib import import_module\nimport_module(name)\nimport_module('.x', 'app')\n")
        self.write("src/other/c10.py", "from app.plugins.loader import load\nimport app.plugins.deep.x\n")
        self.write("src/other/c11.py", "from nsapp.core.engine import go\n")
        self.assertEqual(self.violations(), [
            "src/other/c05.py:2: entrypoint-boundary: imports subpackage app.core; use app's entry points",
            "src/other/c06.py:1: entrypoint-boundary: imports subpackage app.core; use app's entry points",
            "src/other/c10.py:1: entrypoint-boundary: imports subpackage app.plugins; use app's entry points",
            "src/other/c10.py:2: entrypoint-boundary: imports subpackage app.plugins; use app's entry points",
            "src/other/c11.py:1: entrypoint-boundary: imports subpackage nsapp.core; use nsapp's entry points",
        ])

    def test_dynamic_import_cycle(self):
        self.write("pkg/__init__.py")
        self.write("pkg/a.py", "import importlib\nb = importlib.import_module('pkg.b')\n")
        self.write("pkg/b.py", "from pkg import a\n")
        self.assertEqual(self.violations(), ["no-circular: pkg.a -> pkg.b -> pkg.a"])


class Discovery(Scratch):
    def test_monorepo_packages_are_first_party(self):
        self.write("packages/alpha/src/alpha/__init__.py")
        self.write("packages/alpha/src/alpha/_impl/__init__.py")
        self.write("packages/alpha/src/alpha/_impl/core.py", "X = 1\n")
        self.write("packages/beta/src/beta/__init__.py")
        self.write("packages/beta/src/beta/use.py", "from alpha._impl.core import X\n")
        self.write("packages/gamma/gamma/__init__.py")
        self.write("packages/gamma/gamma/sub/__init__.py")
        self.write("packages/gamma/tests/test_g.py", "from gamma.sub import y\nfrom beta import use\n")
        self.assertEqual(self.violations(), [
            "packages/beta/src/beta/use.py:1: entrypoint-boundary: imports private name alpha._impl; use alpha's entry points",
            "packages/gamma/tests/test_g.py:1: tests-through-entrypoints: imports subpackage gamma.sub; use gamma's entry points",
        ])
        self.assertEqual(Repo(self.root).tops, {"alpha", "beta", "gamma"})

    def test_a_packages_folder_that_is_a_regular_package_is_one_top(self):
        self.write("packages/__init__.py")
        self.write("packages/alpha/__init__.py")
        self.write("packages/alpha/core.py")
        self.assertEqual(Repo(self.root).tops, {"packages"})

    def test_src_as_a_package_is_the_top_level_package(self):
        self.write("src/__init__.py")
        self.write("src/core/__init__.py")
        self.write("src/core/engine.py", "def run():\n    pass\n")
        self.write("src/core/api.py", "from .engine import run\n")
        self.write("src/main.py", "from src.core.engine import run\n")
        self.assertEqual(self.violations(), [])
        self.assertEqual(Repo(self.root).tops, {"src"})
        self.write("tests/test_probe.py", "from src._slopbrake_probe import value\n")
        self.assertEqual(self.violations(), [
            "tests/test_probe.py:1: tests-through-entrypoints: imports private name src._slopbrake_probe; use src's entry points",
        ])


class Cycles(Scratch):
    def test_runtime_branches_of_type_checking_guards_are_module_level(self):
        self.write("pkg/__init__.py")
        self.write("pkg/c.py", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    from pkg import d\n")
        self.write("pkg/d.py", "from pkg import c\n")
        self.write("pkg/e.py", "import typing\nif not typing.TYPE_CHECKING:\n    from pkg import f\n")
        self.write("pkg/f.py", "from pkg import e\n")
        self.write("pkg/g.py", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg import h\n")
        self.write("pkg/h.py", "from pkg import g\n")
        self.write("pkg/i.py", "from typing import TYPE_CHECKING\nif not TYPE_CHECKING:\n    pass\nelse:\n    from pkg import j\n")
        self.write("pkg/j.py", "from pkg import i\n")
        self.assertEqual(self.violations(), ["no-circular: pkg.c -> pkg.d -> pkg.c", "no-circular: pkg.e -> pkg.f -> pkg.e"])


class Baseline(Scratch):
    def setUp(self):
        super().setUp()
        for rel in ("__init__", "core/__init__", "core/engine", "core/_secret"):
            self.write(f"app/{rel}.py")
        self.write("tests/test_x.py", "from app.core.engine import run\n")

    def test_new_deep_imports_into_a_baselined_subpackage_still_fail(self):
        self.assertEqual(boundaries(self.root, "--baseline", ".bl", "--write-baseline").returncode, 0)
        self.assertEqual(self.violations("--baseline", ".bl"), [])
        self.write("tests/test_x.py", "from app.core.engine import run\nfrom app.core._secret import K\nimport app.core.engine as e2\n")
        self.assertEqual(self.violations("--baseline", ".bl"), [
            "tests/test_x.py:2: tests-through-entrypoints: imports subpackage app.core; use app's entry points",
            "tests/test_x.py:3: tests-through-entrypoints: imports subpackage app.core; use app's entry points",
        ])
        self.assertIn("tests/test_x.py: tests-through-entrypoints: app.core.engine.run", (self.root / ".bl").read_text())

    def test_old_format_baselines_are_still_read(self):
        self.write(".bl", "tests/test_x.py: tests-through-entrypoints: imports subpackage app.core; use app's entry points\n")
        self.assertEqual(self.violations("--baseline", ".bl"), [])


if __name__ == "__main__":
    unittest.main()
