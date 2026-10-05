"""The README and docs/RULES.md name the rules `slopbrake verify` proves: keep them true."""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def proved_rules() -> set[str]:
    """Rule IDs tagged on the Verifier's prove() calls in cli.py."""
    tree = ast.parse((ROOT / "slopbrake/cli.py").read_text(encoding="utf-8"))
    rules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "prove" and len(node.args) >= 4:
            tag = node.args[3]
            if isinstance(tag, ast.Constant) and isinstance(tag.value, str):
                rules.update(tag.value.split("/"))
    return rules


def claimed_rules(doc: str) -> set[str]:
    """The rule IDs a doc lists after 'proves 10 rules (' or 'the 10 rules a machine can check ('."""
    match = re.search(r"\b10 rules[^(]*\(([^)]*)\)", (ROOT / doc).read_text(encoding="utf-8"))
    return set(re.findall(r"\b[A-Z]\d\b", match.group(1))) if match else set()


class VerifyClaims(unittest.TestCase):
    def test_readme_lists_the_rules_verify_proves(self):
        self.assertEqual(claimed_rules("README.md"), proved_rules())

    def test_rules_doc_lists_the_rules_verify_proves(self):
        self.assertEqual(claimed_rules("docs/RULES.md"), proved_rules())

    def test_ten_rules_are_proved(self):
        self.assertEqual(len(proved_rules()), 10)


if __name__ == "__main__":
    unittest.main()
