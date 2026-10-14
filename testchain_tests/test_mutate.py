import ast
import unittest

from testchain.mutate import mutants_for_source

SRC = b'''"""doc 1"""
LIMIT = 10


def f(a, b, flag):
    """docstring 2"""
    if a < b and not flag:
        return a + b
    total = 0
    total += a
    return f"{a + 1}"


if __name__ == "__main__":
    print(f(1, 2, True))
'''


class TestMutate(unittest.TestCase):
    def setUp(self):
        self.ms = mutants_for_source("pkg/mod.py", SRC)
        self.by_op = {}
        for m in self.ms:
            self.by_op.setdefault(m.operator, []).append(m)

    def test_every_mutant_parses_and_differs(self):
        for m in self.ms:
            out = m.apply(SRC)
            self.assertNotEqual(out, SRC)
            ast.parse(out.decode())

    def test_operator_token_swap_keeps_formatting(self):
        ror = self.by_op["ROR"][0]
        self.assertEqual((ror.original, ror.replacement), ("a < b", "a <= b"))
        aor = [m for m in self.by_op["AOR"] if m.original == "a + b"][0]
        self.assertEqual(aor.replacement, "a - b")

    def test_logical_connector_is_parenthesised(self):
        lcr = self.by_op["LCR"][0]
        self.assertEqual(lcr.replacement, "(a < b or not flag)")

    def test_not_deletion_and_aug_assign(self):
        self.assertEqual(self.by_op["UOD"][0].replacement, "flag")
        self.assertEqual(self.by_op["ASR"][0].replacement, "total -= a")

    def test_skips_docstrings_fstrings_and_main_block(self):
        lines = {m.lineno for m in self.ms}
        self.assertNotIn(1, lines)   # module docstring
        self.assertNotIn(6, lines)   # function docstring
        self.assertNotIn(11, [m.lineno for m in self.ms if m.operator == "AOR"])  # inside f-string
        self.assertFalse(any(m.lineno >= 14 for m in self.ms))  # __main__ block

    def test_diff_is_one_line_change(self):
        m = self.by_op["ROR"][0]
        diff = m.diff(SRC)
        self.assertEqual(sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")), 1)
        self.assertIn("+    if a <= b and not flag:", diff)

    def test_pragma_skips_line(self):
        ms = mutants_for_source("m.py", b"X = 1  # pragma: no mutate\nY = 2\n")
        self.assertEqual([m.original for m in ms], ["2"])

    def test_ids_are_sequential_from_start_index(self):
        ms = mutants_for_source("m.py", b"X = 1\nY = 2\n", start_index=7)
        self.assertEqual([m.id for m in ms], ["M0007", "M0008"])


if __name__ == "__main__":
    unittest.main()
