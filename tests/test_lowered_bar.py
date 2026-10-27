"""Behaviour tests for the lowered-bar check (lowered_bar.py): rule T6, no silent lowering of the bar.

Each test builds a repo on main, branches, changes it, and runs the check against main (the base the other
base-relative checks use). Only lines the change adds can fail; what was already there never does.
"""
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

GUARDS = Path(__file__).resolve().parents[1] / "slopbrake/kit/common/scripts/slopbrake"
sys.path.insert(0, str(GUARDS))
from common import parse_unified_diff

GIT_ID = ["-c", "user.name=kit-test", "-c", "user.email=kit@test"]


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


class Repo(unittest.TestCase):
    """A repo whose main holds `base` files; the test then edits the working tree of branch `feat`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sh(["git", "init", "-q", "-b", "main"], self.root)
        self.write("README.md", "# demo\n")

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def commit(self, message="change"):
        sh(["git", "add", "-A"], self.root)
        result = sh(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", message], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def base(self, files):
        for rel, text in files.items():
            self.write(rel, text)
        self.commit("base")
        sh(["git", "switch", "-q", "-c", "feat"], self.root)

    def run_check(self, *args):
        return sh([sys.executable, str(GUARDS / "lowered_bar.py"), "--base", "main", *args], self.root)

    def findings(self, *args):
        """(path, line, text) for every T6 finding."""
        result = self.run_check(*args)
        out = []
        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit() and parts[2].startswith(" T6 "):
                out.append((parts[0], int(parts[1]), parts[2].strip()))
        return out

    def lines(self, *args):
        return [(path, line) for path, line, _ in self.findings(*args)]


PY_TEST = """\
    import unittest


    class Cart(unittest.TestCase):
        def test_total(self):
            self.assertEqual(sum([1, 2]), 3)

        def test_empty(self):
            self.assertEqual(sum([]), 0)
    """


class PythonSkips(Repo):
    def test_a_new_skip_decorator_fails_naming_the_rule_the_line_and_the_remedy(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.replace(
            "    def test_total", "    @unittest.skip('flaky')\n        def test_total"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("tests/test_cart.py:5: T6 skipped test added: unittest.skip;", result.stdout)
        self.assertIn("slopbrake: allow-skip: <reason>", result.stdout)
        self.assertIn("lowered-bar: 1 skipped test(s)", result.stdout)

    def test_every_python_skip_spelling_on_an_added_line_fails(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_skips.py", """\
            import sys
            import unittest
            from unittest import skipIf

            import pytest

            pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix only")


            @pytest.mark.skip(reason="later")
            def test_a():
                assert 1 + 1 == 2


            @pytest.mark.xfail
            def test_b():
                assert 1 + 1 == 2


            def test_c():
                pytest.skip("not today")


            class T(unittest.TestCase):
                @unittest.skipIf(True, "x")
                def test_d(self):
                    self.assertEqual(1 + 1, 2)

                @skipIf(True, "x")
                def test_e(self):
                    self.assertEqual(1 + 1, 2)

                def test_f(self):
                    self.skipTest("later")

                @unittest.expectedFailure
                def test_g(self):
                    self.assertEqual(1 + 1, 3)
            """)
        self.assertEqual([line for _, line in self.lines()], [7, 10, 15, 21, 25, 29, 34, 36])

    def test_a_skip_already_on_main_never_fails_when_its_file_changes(self):
        skipped = PY_TEST.replace("    def test_empty", "    @unittest.skip('upstream bug')\n        def test_empty")
        self.base({"tests/test_cart.py": skipped})
        self.write("tests/test_cart.py", skipped.replace("sum([1, 2]), 3", "sum([1, 2, 3]), 6"))
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("lowered-bar: ok", result.stdout)

    def test_a_skip_reworded_in_place_is_not_a_new_skip(self):
        skipped = PY_TEST.replace("    def test_empty", "    @unittest.skip('upstream bug')\n        def test_empty")
        self.base({"tests/test_cart.py": skipped})
        self.write("tests/test_cart.py", skipped.replace("upstream bug", "upstream bug #12"))
        self.assertEqual(self.lines(), [])

    def test_skip_words_inside_strings_and_comments_are_not_skips(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_docs.py", '''\
            """Never write @pytest.mark.skip or self.skipTest( to make a suite green."""

            # pytest.skip( is how a test gives up; we do not.
            def test_message():
                assert render() == "use @unittest.skip('why') and pytest.mark.xfail sparingly"
            ''')
        self.assertEqual(self.lines(), [])

    def test_a_reasoned_allow_on_the_line_or_the_line_above_passes(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.replace(
            "    def test_total", "    # slopbrake: allow-skip: the payment sandbox is down until 1 Nov\n"
                                  "        @unittest.skip('sandbox')\n        def test_total").replace(
            "    def test_empty", "    @unittest.skip('x')  # slopbrake: allow-skip: tracked in #41\n"
                                  "        def test_empty"))
        self.assertEqual(self.lines(), [])

    def test_an_allow_without_a_reason_is_not_an_allow(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.replace(
            "    def test_total", "    @unittest.skip('x')  # slopbrake: allow-skip:\n        def test_total"))
        self.assertEqual(self.lines(), [("tests/test_cart.py", 5)])

    def test_an_allow_skip_does_not_excuse_a_suppression(self):
        self.base({"src/app.py": "def f(x):\n    return x\n"})
        self.write("src/app.py", "def f(x):\n    return x  # noqa: E501  # slopbrake: allow-skip: wrong kind\n")
        self.assertEqual(self.lines(), [("src/app.py", 2)])


class PythonSuppressions(Repo):
    def test_every_python_suppression_on_an_added_line_fails(self):
        self.base({"src/app.py": "def f(x):\n    return x\n"})
        self.write("src/app.py", """\
            import os  # noqa
            import sys  # noqa: F401
            def f(x):
                return x  # type: ignore[return-value]
            def g(x):  # pyright: ignore
                return x  # pragma: no cover
            def h(x):  # pylint: disable=unused-argument
                return 1  # NOQA:E501,W291
            """)
        found = self.findings()
        self.assertEqual([line for _, line, _ in found], [1, 2, 4, 5, 6, 7, 8])
        self.assertIn("T6 suppression added: # noqa: F401;", found[1][2])
        self.assertIn("slopbrake: allow-suppress: <reason>", found[1][2])

    def test_two_suppressions_on_one_line_are_two_findings(self):
        self.base({"src/app.py": "def f(x):\n    return x\n"})
        self.write("src/app.py", "def f(x):\n    return x  # type: ignore  # noqa: E501\n")
        self.assertEqual(self.lines(), [("src/app.py", 2), ("src/app.py", 2)])

    def test_an_existing_suppression_on_a_reformatted_line_is_not_new(self):
        self.base({"src/app.py": "def f(x):\n    return g(x,1)  # noqa: E501\n"})
        self.write("src/app.py", "def f(x):\n    return g(x, 1)  # noqa: E501\n")
        self.assertEqual(self.lines(), [])

    def test_a_suppression_moved_to_another_file_is_not_new(self):
        self.base({"src/app.py": "def f(x):\n    return g(x)  # type: ignore[arg-type]\n", "src/other.py": "X = 1\n"})
        self.write("src/app.py", "def f(x):\n    return x\n")
        self.write("src/other.py", "X = 1\n\n\ndef f(x):\n    return g(x)  # type: ignore[arg-type]\n")
        self.assertEqual(self.lines(), [])

    def test_widening_a_scoped_suppression_to_a_blanket_one_fails(self):
        self.base({"src/app.py": "def f(x):\n    return g(x)  # noqa: E501\n"})
        self.write("src/app.py", "def f(x):\n    return g(x)  # noqa\n")
        self.assertEqual(self.lines(), [("src/app.py", 2)])

    def test_suppression_words_in_strings_and_prose_comments_are_not_suppressions(self):
        self.base({"tests/test_lint.py": "def test_x():\n    assert 1 + 1 == 2\n"})
        self.write("tests/test_lint.py", '''\
            def test_x():
                assert 1 + 1 == 2
            # we never add a noqa or a type: ignore here
            def test_message():
                assert lint("x = 1  # noqa") == "x = 1  # type: ignore"
                assert hint() == """# pragma: no cover
            # pyright: ignore"""
            ''')
        self.assertEqual(self.lines(), [])

    def test_a_reasoned_allow_suppress_passes(self):
        self.base({"src/app.py": "def f(x):\n    return x\n"})
        self.write("src/app.py", "def f(x):\n    return x  # noqa: E501  # slopbrake: allow-suppress: the URL can't wrap\n")
        self.assertEqual(self.lines(), [])

    def test_a_coding_cookie_and_a_bom_keep_line_numbers_and_decoding_right(self):
        self.base({"src/latin.py": "# -*- coding: latin-1 -*-\nNAME = 'x'\n", "src/bom.py": "X = 1\n"})
        (self.root / "src/latin.py").write_bytes(
            "# -*- coding: latin-1 -*-\nNAME = 'café'\nY = 2  # noqa\n".encode("latin-1"))
        (self.root / "src/bom.py").write_bytes("\ufeffX = 1\nY = 2  # type: ignore\n".encode())
        self.assertEqual(self.lines(), [("src/bom.py", 2), ("src/latin.py", 3)])

    def test_a_python_file_that_does_not_parse_is_still_scanned(self):
        self.base({"src/app.py": "X = 1\n"})
        self.write("src/app.py", "X = (1\nY = 2  # noqa\nZ = '# noqa'\n")
        self.assertEqual(self.lines(), [("src/app.py", 2)])


TS_TEST = textwrap.dedent("""\
    import { expect, it, describe } from "vitest";

    describe("cart", () => {
      it("adds two numbers", () => {
        expect(1 + 1).toBe(2);
      });

      it("starts empty", () => {
        expect([].length).toBe(0);
      });
    });
    """)


class TypeScriptSkipsAndSuppressions(Repo):
    def test_every_skip_and_focus_spelling_on_an_added_line_fails(self):
        self.base({"tests/cart.test.ts": TS_TEST})
        self.write("tests/more.test.ts", """\
            import { expect, it, test, describe } from "vitest";
            it.skip("a", () => { expect(1).toBe(1); });
            test.skip("b", () => { expect(1).toBe(1); });
            describe.skip("c", () => {});
            xit("d", () => { expect(1).toBe(1); });
            xdescribe("e", () => {});
            it.todo("f");
            it.only("g", () => { expect(1).toBe(1); });
            describe . only("h", () => {});
            test.describe.skip("i", () => {});
            it.skipIf(process.env.CI)("j", () => { expect(1).toBe(1); });
            """)
        found = self.findings()
        self.assertEqual([line for _, line, _ in found], [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        self.assertIn("T6 skipped test added: it.skip;", found[0][2])
        self.assertIn("T6 focused test added: it.only (every other test", found[6][2])
        self.assertIn("T6 focused test added: describe.only", found[7][2])

    def test_every_typescript_suppression_on_an_added_line_fails(self):
        self.base({"src/app.ts": "export const x = 1;\n"})
        self.write("src/app.ts", """\
            export const x = 1;
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            export const y: any = 2; // eslint-disable-line no-console
            /* eslint-disable */
            // @ts-ignore
            export const z: number = "3";
            // @ts-expect-error -- legacy
            /** @ts-nocheck */
            /* istanbul ignore next */
            /* c8 ignore next 3 */
            /* v8 ignore start */
            /* c8 ignore stop */
            /* eslint-enable */
            """)
        found = self.findings()
        self.assertEqual([line for _, line, _ in found], [2, 3, 4, 5, 7, 8, 9, 10, 11])
        self.assertIn("T6 suppression added: eslint-disable-next-line @typescript-eslint/no-explicit-any;", found[0][2])

    def test_a_skip_or_suppression_already_on_main_never_fails(self):
        skipped = TS_TEST.replace('it("starts empty"', 'it.skip("starts empty"').replace(
            "  it(\"adds", "  // eslint-disable-next-line no-console\n  it(\"adds")
        self.base({"tests/cart.test.ts": skipped})
        self.write("tests/cart.test.ts", skipped.replace("expect(1 + 1).toBe(2)", "expect(2 + 2).toBe(4)"))
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_skip_and_suppression_words_in_strings_templates_and_regexes_are_not_flagged(self):
        self.base({"tests/cart.test.ts": TS_TEST})
        self.write("tests/lint.test.ts", """\
            import { expect, it } from "vitest";
            it("reports focus", () => {
              const source = 'it.only("x")';
              const banner = `// eslint-disable ${source} xit(`;
              const pattern = /it\\.skip\\(|@ts-ignore/;
              const half = 4 / 2; const other = 6 / 3;
              expect(lint("describe.skip('y') // @ts-ignore")).toEqual([source, banner, pattern, half, other]);
            });
            """)
        self.assertEqual(self.lines(), [])

    def test_skip_spellings_in_source_outside_the_test_tree_are_ordinary_calls(self):
        self.base({"src/view.ts": "export const x = 1;\n", "tests/cart.test.ts": TS_TEST})
        self.write("src/view.ts", "export const x = 1;\nfit(terminal);\ncontext.skip(frame);\n")
        self.write("tests/view.test.ts", TS_TEST.replace('it("adds two numbers"', 'fit("fits the terminal"'))
        self.assertEqual(self.lines(), [("tests/view.test.ts", 4)])

    def test_reasoned_allows_pass_in_typescript(self):
        self.base({"tests/cart.test.ts": TS_TEST, "src/app.ts": "export const x = 1;\n"})
        self.write("tests/cart.test.ts", TS_TEST.replace(
            'it("starts empty"', 'it.skip("starts empty"').replace(
            "  it.skip(", "  // slopbrake: allow-skip: waits on the cart rewrite\n  it.skip("))
        self.write("src/app.ts", "export const x = 1;\n"
                                 "// eslint-disable-next-line no-console -- slopbrake: allow-suppress: CLI output\n"
                                 "console.log(x);\n")
        self.assertEqual(self.lines(), [])


class DeletedTests(Repo):
    def test_a_deleted_python_test_fails_naming_it_and_where_it_was(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.split("    def test_empty")[0])
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("tests/test_cart.py:8: T6 test deleted: test_empty", result.stdout)
        self.assertIn("slopbrake: allow-delete-test: test_empty: <reason>", result.stdout)

    def test_a_renamed_test_with_the_same_body_is_not_a_deletion(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.replace("test_empty", "test_an_empty_cart_totals_zero"))
        self.assertEqual(self.lines(), [])

    def test_a_test_moved_to_another_file_is_not_a_deletion(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.split("    def test_empty")[0])
        self.write("tests/unit/test_empty.py", "def test_empty():\n    assert sum([]) == 0\n")
        self.assertEqual(self.lines(), [])

    def test_a_renamed_test_file_keeps_its_tests(self):
        self.base({"tests/test_cart.py": PY_TEST})
        sh(["git", "mv", "tests/test_cart.py", "tests/test_basket.py"], self.root)
        self.assertEqual(self.lines(), [])

    def test_a_deleted_test_file_fails_for_each_of_its_tests(self):
        self.base({"tests/test_cart.py": PY_TEST, "tests/test_other.py": "def test_x():\n    assert 1 + 1 == 2\n"})
        (self.root / "tests/test_cart.py").unlink()
        self.assertEqual(self.lines(), [("tests/test_cart.py", 5), ("tests/test_cart.py", 8)])

    def test_a_test_whose_name_is_left_elsewhere_still_counts_when_one_copy_goes(self):
        same = "def test_basic():\n    assert 1 + 1 == 2\n"
        self.base({"tests/test_a.py": same, "tests/test_b.py": "def test_basic():\n    assert 2 + 2 == 4\n"})
        (self.root / "tests/test_b.py").unlink()
        self.assertEqual(self.lines(), [("tests/test_b.py", 1)])

    def test_committed_deletions_on_the_branch_count(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.split("    def test_empty")[0])
        self.commit("drop a test")
        self.assertEqual(self.lines(), [("tests/test_cart.py", 8)])

    def test_an_allow_delete_line_in_the_test_tree_passes(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.split("    def test_empty")[0]
                   + "# slopbrake: allow-delete-test: test_empty: covered by test_total's empty case\n")
        self.assertEqual(self.lines(), [])

    def test_an_allow_delete_for_another_name_or_without_a_reason_does_not_pass(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.split("    def test_empty")[0]
                   + "# slopbrake: allow-delete-test: test_other: gone\n# slopbrake: allow-delete-test: test_empty:\n")
        self.assertEqual(self.lines(), [("tests/test_cart.py", 8)])

    def test_an_allow_delete_outside_the_test_tree_does_not_pass(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.split("    def test_empty")[0])
        self.write("src/app.py", "# slopbrake: allow-delete-test: test_empty: nobody needs it\n")
        self.assertEqual(self.lines(), [("tests/test_cart.py", 8)])

    def test_an_allow_delete_in_the_pr_body_passes(self):
        self.base({"tests/test_cart.py": PY_TEST})
        self.write("tests/test_cart.py", PY_TEST.split("    def test_empty")[0])
        body = self.write("body.md", "## Summary\n\nslopbrake: allow-delete-test: test_empty: duplicate of test_total\n")
        self.assertEqual(self.lines("--body-file", str(body)), [])

    def test_a_deleted_typescript_case_fails_and_a_renamed_one_does_not(self):
        self.base({"tests/cart.test.ts": TS_TEST})
        self.write("tests/cart.test.ts", TS_TEST.replace('it("adds two numbers"', 'it("adds two prices"').replace(
            '  it("starts empty", () => {\n    expect([].length).toBe(0);\n  });\n', ""))
        found = self.findings()
        self.assertEqual([(path, line) for path, line, _ in found], [("tests/cart.test.ts", 8)])
        self.assertIn("T6 test deleted: starts empty", found[0][2])

    def test_a_typescript_allow_delete_with_spaces_and_colons_in_the_name_passes(self):
        self.base({"tests/cart.test.ts": TS_TEST.replace("starts empty", "cart: starts empty")})
        self.write("tests/cart.test.ts", TS_TEST.replace(
            '  it("starts empty", () => {\n    expect([].length).toBe(0);\n  });\n',
            "  // slopbrake: allow-delete-test: cart: starts empty: the constructor test covers it\n"))
        self.assertEqual(self.lines(), [])

    def test_helpers_and_non_test_files_are_not_tests(self):
        self.base({"tests/helpers.py": "def test_data():\n    return [1]\n", "src/app.py": "def test_mode():\n    return 1\n"})
        (self.root / "tests/helpers.py").unlink()
        (self.root / "src/app.py").unlink()
        self.assertEqual(self.lines(), [])


class Scope(Repo):
    def test_committed_changes_since_the_merge_base_are_measured(self):
        self.base({"src/app.py": "X = 1\n"})
        self.write("src/app.py", "X = 1  # noqa\n")
        self.commit()
        self.assertEqual(self.lines(), [("src/app.py", 1)])

    def test_changes_on_main_after_the_branch_point_are_not_the_branchs(self):
        self.base({"src/app.py": "X = 1\n"})
        sh(["git", "switch", "-q", "main"], self.root)
        self.write("src/other.py", "Y = 2  # noqa\n")
        self.commit("main moves on")
        sh(["git", "switch", "-q", "feat"], self.root)
        self.write("src/app.py", "X = 2\n")
        self.assertEqual(self.run_check().returncode, 0)

    def test_nothing_relevant_changed_is_a_skip_not_a_pass(self):
        self.base({"src/app.py": "X = 1\n"})
        self.write("README.md", "# demo, now with # noqa in prose\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 78, result.stdout)
        self.assertIn("lowered-bar: skipped: no changed Python or JavaScript/TypeScript files since", result.stdout)

    def test_the_kit_scripts_and_excluded_paths_are_not_scanned(self):
        self.base({"src/app.py": "X = 1\n"})
        self.write("scripts/slopbrake/extra.py", "X = 1  # noqa\n")
        self.write("vendor/lib.py", "X = 1  # noqa\n")
        self.write("node_modules/pkg/index.js", "// eslint-disable\n")
        self.assertEqual(self.lines("--exclude", "vendor/"), [])

    def test_the_output_names_the_base(self):
        self.base({"src/app.py": "X = 1\n"})
        self.write("src/app.py", "X = 2\n")
        result = self.run_check()
        self.assertIn("lowered-bar: ok (base main", result.stdout)


class DiffParsing(unittest.TestCase):
    def test_removed_lines_are_recorded_with_their_old_line_numbers(self):
        diff = textwrap.dedent("""\
            diff --git a/x.py b/x.py
            --- a/x.py
            +++ b/x.py
            @@ -2,2 +2 @@
            -old two  # noqa
            --- a removed line that looks like a header
            +new two
            @@ -9 +8,0 @@
            -old nine
            """)
        change = parse_unified_diff(diff)["x.py"]
        self.assertEqual(change.removed, {2: "old two  # noqa", 3: "-- a removed line that looks like a header",
                                          9: "old nine"})
        self.assertEqual(change.added, {2: "new two"})


if __name__ == "__main__":
    unittest.main()
