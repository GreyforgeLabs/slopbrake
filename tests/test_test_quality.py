"""Behaviour tests for the test-quality check (tautology_py.py): rule T1 and the no-assertion rule."""
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

GUARDS = Path(__file__).resolve().parents[1] / "slopbrake/kit/common/scripts/slopbrake"
GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def check(self, *args):
        return sh([sys.executable, str(GUARDS / "tautology_py.py"), *args], self.root)

    def flagged(self, source, kind="tautological", name="tests/test_seed.py"):
        self.write(name, source)
        result = self.check("tests" if name.startswith("tests/") else name)
        return sorted(int(line.split(":")[1]) for line in result.stdout.splitlines() if f": {kind}" in line)


class AggregateDirectForm(Scratch):
    def test_aggregate_written_inline_is_flagged(self):
        self.assertEqual(self.flagged("""\
            from shop import longest, total
            def test_a():
                prices = [3, 4, 5]
                assert total(prices) == sum(prices)
            def test_b():
                words = ["a", "bbb"]
                assert longest(words) == max(words, key=len)
            def test_c():
                items = [{"price": 10}, {"price": 5}]
                assert total(items) == sum(i["price"] for i in items)
            """), [4, 7, 10])

    def test_aggregate_applied_to_both_sides_is_a_property(self):
        self.assertEqual(self.flagged("""\
            from shop import double, sort_all
            def test_a():
                xs = [3, 1, 2]
                assert len(double(xs)) == len(xs)
                assert sorted(sort_all(xs)) == sorted(xs)
            """), [])

    def test_builtin_type_methods_are_not_the_code_under_test(self):
        self.assertEqual(self.flagged("""\
            from shop import frame
            def test_a():
                wire = frame(b"abc")
                assert int.from_bytes(wire[:8], "big") == len(wire) - 8
            """), [])


class LiteralShapes(Scratch):
    def test_literal_comparisons_and_literal_unittest_asserts_are_flagged(self):
        self.assertEqual(self.flagged("""\
            import unittest
            from shop import f
            def test_a():
                assert 2 > 1
                assert not False
                assert 1 != 2
            class T(unittest.TestCase):
                def test_b(self):
                    self.assertIn(1, [1, 2])
                    self.assertNotIn(3, [1, 2])
                    self.assertNotEqual(1, 2)
                    self.assertGreater(2, 1)
                    self.assertGreaterEqual(2, 2)
                    self.assertLess(1, 2)
                    self.assertLessEqual(1, 2)
                    self.assertTrue(f() == f())
                    self.assertTrue(1 == 1)
            """), [4, 5, 6, 9, 10, 11, 12, 13, 14, 15, 16, 17])

    def test_assert_true_on_a_comparison_is_judged_like_assert(self):
        self.assertEqual(self.flagged("""\
            import unittest
            from shop import add
            class T(unittest.TestCase):
                def test_a(self):
                    a, b = 2, 3
                    self.assertTrue(add(a, b) == a + b)
                    self.assertTrue(add(2, 3) == 5)
                    self.assertIn(add(a, b), [5])
            """), [6])


class SelfComparison(Scratch):
    def test_identity_and_determinism_checks_pass(self):
        self.assertEqual(self.flagged("""\
            import unittest
            from shop import cache, get_instance, rng
            def test_a():
                assert cache.get("k") is cache.get("k")
                assert rng(seed=1) == rng(seed=1)
            class T(unittest.TestCase):
                def test_b(self):
                    self.assertIs(get_instance(), get_instance())
            """), [])

    def test_restating_without_code_under_test_is_still_flagged(self):
        self.assertEqual(self.flagged("""\
            from shop import f
            def test_a():
                items = [10, 5]
                assert sum(items) == sum(items)
                assert f() == f()
            """), [4, 5])


class FormatStringSpec(Scratch):
    TEST = """\
        from shop import greet
        def test_a():
            name = "Ada"
            assert greet(name) == f"Hello, {name}!"
        """

    def test_spec_expectation_passes_when_the_implementation_differs(self):
        self.write("shop.py", 'def greet(who):\n    return "Hello, " + who + "!"\n')
        self.assertEqual(self.flagged(self.TEST), [])

    def test_flagged_when_it_is_literally_the_implementation(self):
        self.write("shop.py", 'def greet(who):\n    return f"Hello, {who}!"\n')
        self.assertEqual(self.flagged(self.TEST), [4])


class UnittestFilePattern(Scratch):
    def test_tests_py_and_test_star_files_are_scanned(self):
        self.write("app/tests.py", "def test_a():\n    assert True\n")
        self.write("app/testcart.py", "def test_a():\n    assert True\n")
        self.write("app/helpers.py", "def test_a():\n    assert True\n")
        result = self.check(".")
        self.assertEqual(sorted(line.split(":")[0] for line in result.stdout.splitlines() if ": tautological" in line),
                         ["app/testcart.py", "app/tests.py"])


class NoAssertion(Scratch):
    def test_tests_without_an_assertion_fail(self):
        self.write("tests/test_seed.py", """\
            import unittest
            from shop import add
            def test_a():
                add(1, 2)
            class T(unittest.TestCase):
                def test_b(self):
                    add(1, 2)
                    def inner():
                        assert add(1, 2) == 3
            class TestPlain:
                def test_c(self):
                    add(1, 2)
            """)
        result = self.check("tests")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([line.split(":")[1] for line in result.stdout.splitlines() if ": no assertion" in line],
                         ["3", "6", "11"])
        self.assertIn("test-quality: 3 test(s) with no assertion", result.stdout)

    def test_every_assertion_form_counts(self):
        self.assertEqual(self.flagged("""\
            import unittest
            import pytest
            from pytest import raises
            from shop import add, mailer
            def check(x):
                assert x == 3
            def test_a():
                assert add(1, 2) == 3
            def test_b():
                with pytest.raises(ValueError):
                    add(None, 2)
            def test_c():
                with raises(ValueError):
                    add(None, 2)
            def test_d():
                pytest.fail("not yet")
            def test_e():
                mailer.send.assert_called_once_with("x")
            def test_f():
                expect(add(1, 2)).to_equal(3)
            def test_g():
                check(add(1, 2))
            def test_h():
                with pytest.warns(UserWarning):
                    add(1, 2)
            class T(unittest.TestCase):
                def check(self, x):
                    self.assertEqual(x, 3)
                def test_i(self):
                    self.fail("todo")
                def test_j(self):
                    with self.assertRaises(ValueError):
                        add(None, 2)
                def test_k(self):
                    self.check(add(1, 2))
            """, kind="no assertion"), [])

    def test_helpers_count_one_level_only(self):
        self.assertEqual(self.flagged("""\
            from shop import add
            def check(x):
                assert x == 3
            def outer(x):
                check(x)
            def test_a():
                outer(add(1, 2))
            """, kind="no assertion"), [6])

    def test_skipped_tests_and_suppressed_tests_are_ignored(self):
        self.assertEqual(self.flagged("""\
            import unittest
            import pytest
            from shop import add
            @pytest.mark.skip(reason="flaky upstream")
            def test_a():
                add(1, 2)
            class T(unittest.TestCase):
                @unittest.skip("later")
                def test_b(self):
                    add(1, 2)
            @unittest.skip("whole class")
            class U(unittest.TestCase):
                def test_c(self):
                    add(1, 2)
            # slopbrake: allow-no-assert: smoke test, crashing is the failure
            def test_d():
                add(1, 2)
            def test_e():  # slopbrake: allow-no-assert: import smoke
                add(1, 2)
            # slopbrake: allow-no-assert:
            def test_f():
                add(1, 2)
            def helper_not_a_test():
                add(1, 2)
            def test_case(lines):
                return {"inputs": lines}
            """, kind="no assertion"), [21])

    def test_changed_mode_reports_a_test_whose_body_changed(self):
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("tests/test_seed.py", "from shop import add\n\n\ndef test_a():\n    x = add(1, 2)\n    assert x == 3\n\n\n"
                                         "def test_b():\n    add(1, 2)\n")
        sh(["git", "add", "-A"], self.root)
        sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", "init"], self.root)
        self.write("tests/test_seed.py", "from shop import add\n\n\ndef test_a():\n    x = add(2, 2)\n\n\n"
                                         "def test_b():\n    add(1, 2)\n")
        result = self.check("--changed", "--base", "HEAD", "tests")
        self.assertEqual([line.split(":")[1] for line in result.stdout.splitlines() if ": no assertion" in line], ["4"])


if __name__ == "__main__":
    unittest.main()
