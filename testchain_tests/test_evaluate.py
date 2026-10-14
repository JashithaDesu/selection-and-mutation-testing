"""Evaluation logic on a hand-built miniature project where every number can
be worked out on paper."""

import json
import tempfile
import unittest
from pathlib import Path

from testchain import evaluate, llm
from testchain.mutate import Mutant
from testchain.strategies import Context, coverage_based, file_coverage, module_map

U1 = "tests.unit.test_a.T.test_1"
U2 = "tests.unit.test_a.T.test_2"
I1 = "tests.integration.test_b.T.test_1"
E1 = "tests.e2e.test_c.T.test_1"

BASE = {
    "tests": [U1, U2, I1, E1],
    "stages": {U1: "unit", U2: "unit", I1: "integration", E1: "e2e"},
    "durations": {U1: 1.0, U2: 1.0, I1: 2.0, E1: 4.0},
    "class_overhead": {"tests.unit.test_a.T": 0.0, "tests.integration.test_b.T": 0.0,
                       "tests.e2e.test_c.T": 2.0},
    "coverage": {U1: {"sut/a.py": [3]}, U2: {"sut/a.py": [5]}, I1: {"sut/a.py": [3], "sut/b.py": [1]}, E1: {}},
    "imports": {"tests.unit.test_a": ["sut.a"], "tests.integration.test_b": ["sut.a", "sut.b"], "tests.e2e.test_c": []},
}


def mutant(mid, file, line):
    return {"id": mid, "file": file, "lineno": line, "end_lineno": line}


class TestStrategies(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(BASE)

    def test_coverage_selects_tests_hitting_line(self):
        self.assertEqual(coverage_based(self.ctx, mutant("M1", "sut/a.py", 3)), [U1, I1])
        self.assertEqual(coverage_based(self.ctx, mutant("M1", "sut/a.py", 99)), [])

    def test_file_coverage_and_module_map(self):
        self.assertEqual(file_coverage(self.ctx, mutant("M1", "sut/a.py", 99)), [U1, U2, I1])
        self.assertEqual(module_map(self.ctx, mutant("M1", "sut/b.py", 1)), [I1])

    def test_simulate_charges_fixture_once_and_reports_feedback(self):
        cost, first = evaluate.simulate(self.ctx, [U1, I1, E1], {E1})
        self.assertEqual(cost, 1 + 2 + (2 + 4))
        self.assertEqual(first, 9.0)
        cost, first = evaluate.simulate(self.ctx, [U1], {E1})
        self.assertEqual((cost, first), (1.0, None))


class TestEvaluate(unittest.TestCase):
    def test_end_to_end_numbers(self):
        mutants = [mutant("M1", "sut/a.py", 3), mutant("M2", "sut/a.py", 5), mutant("M3", "sut/c.py", 1),
                   mutant("M4", "sut/a.py", 3)]
        km = {"M1": {"status": "killed", "killed_by": [U1]},
              "M2": {"status": "killed", "killed_by": [U2, E1]},
              "M3": {"status": "killed", "killed_by": [E1]},       # only e2e sees it
              "M4": {"status": "survived", "killed_by": []}}
        ev = evaluate.evaluate(BASE, mutants, km, random_seeds=3)
        self.assertEqual(ev["population"], {"mutants_total": 4, "detectable": 3})
        self.assertEqual(ev["run_all_seconds"], 10.0)
        s = ev["strategies"]
        self.assertAlmostEqual(s["coverage"]["detection_rate"], 2 / 3)
        self.assertAlmostEqual(s["unit_only"]["detection_rate"], 2 / 3)
        self.assertEqual(s["run_all"]["detected"], 3)
        self.assertEqual(ev["stage_profile"]["first_stage_counts"], {"unit": 2, "integration": 0, "e2e": 1})
        self.assertEqual(ev["stage_profile"]["only_detected_by_e2e"], ["M3"])
        self.assertEqual(ev["adequacy"]["survivors"], ["M4"])
        self.assertEqual(ev["test_value"][U1]["unique_kills"], 1)
        # run_all ROI has nothing to break even against; a strategy that misses must have a finite break-even
        self.assertTrue(0 < ev["roi"]["coverage"]["break_even_escape_cost"] < float("inf"))

    def test_llm_run_is_merged_with_latency(self):
        mutants = [mutant("M1", "sut/a.py", 3)]
        km = {"M1": {"status": "killed", "killed_by": [E1]}}
        run = {"M1": {"selected": [E1], "hallucinated": ["tests.fake"], "parse_ok": True, "latency_s": 1.5, "error": None}}
        ev = evaluate.evaluate(BASE, mutants, km, llm_runs={"fake": run}, random_seeds=2)
        s = ev["strategies"]["llm:fake"]
        self.assertEqual(s["detected"], 1)
        self.assertAlmostEqual(s["mean_cost_s"], 6.0 + 1.5)
        self.assertEqual(ev["llm"]["fake"]["hallucinated_ids_total"], 1)


class TestSampledLLM(unittest.TestCase):
    def test_partial_run_compared_on_same_subset(self):
        mutants = [mutant("M1", "sut/a.py", 3), mutant("M2", "sut/a.py", 5)]
        km = {"M1": {"status": "killed", "killed_by": [U1]}, "M2": {"status": "killed", "killed_by": [E1]}}
        run = {"M2": {"selected": [E1], "hallucinated": [], "parse_ok": True, "latency_s": 0.0, "error": None}}
        ev = evaluate.evaluate(BASE, mutants, km, llm_runs={"s": run}, random_seeds=2)
        self.assertNotIn("llm:s", ev["strategies"])
        sub = ev["llm_subsets"]["s"]
        self.assertEqual(sub["n"], 1)
        self.assertEqual(sub["strategies"]["llm:s"]["detected"], 1)
        self.assertEqual(sub["strategies"]["coverage"]["detected"], 0)


class TestCopiedAnswers(unittest.TestCase):
    def test_identical_answers_are_flagged(self):
        mutants = [mutant(f"M{i}", "sut/a.py", 3) for i in range(6)]
        km = {m["id"]: {"status": "killed", "killed_by": [U1]} for m in mutants}
        run = {m["id"]: {"selected": [U1, E1], "hallucinated": [], "parse_ok": True,
                         "latency_s": 1.0, "error": None} for m in mutants}
        ev = evaluate.evaluate(BASE, mutants, km, llm_runs={"copy": run}, random_seeds=2)
        self.assertEqual(ev["llm"]["copy"]["most_common_selection_share"], 1.0)
        self.assertIn("invalid", ev["llm"]["copy"]["warning"])

    def test_prompt_has_no_example_numbers(self):
        self.assertNotRegex(llm.SYSTEM_PROMPT, r"\[\s*\d")


class TestLLM(unittest.TestCase):
    KNOWN = [U1, U2, I1, E1]

    def test_parse_accepts_exact_and_unambiguous_suffix(self):
        sel, hall, ok = llm.parse_selection(
            'Sure! {"selected": ["%s", "test_c.T.test_1", "T.test_1", "made.up"], "reason": "x"}' % U2, self.KNOWN)
        self.assertTrue(ok)
        self.assertEqual(sel, sorted([U2, E1]))
        self.assertEqual(hall, ["T.test_1", "made.up"])   # ambiguous suffix is not guessed

    def test_parse_catalogue_numbers(self):
        sel, hall, ok = llm.parse_selection('{"selected": [1, "4", 99, 0, true]}', self.KNOWN)
        self.assertTrue(ok)
        self.assertEqual(sel, sorted([U1, E1]))
        self.assertEqual(hall, ["99", "0"])   # outside the 1..4 catalogue: invented

    def test_prompt_is_numbered_and_compact(self):
        cat = [(t, t.split(".")[1], "") for t in self.KNOWN]
        prompt = llm.build_prompt("--- a/x\n+++ b/x", cat)
        self.assertIn("1. [unit] test_a.T.test_1", prompt)
        self.assertIn("4. [e2e] test_c.T.test_1", prompt)
        self.assertNotIn("tests.unit.", prompt)

    def test_parse_failures(self):
        self.assertEqual(llm.parse_selection("no json here", self.KNOWN), ([], [], False))
        self.assertEqual(llm.parse_selection('{"selected": "U1"}', self.KNOWN), ([], [], False))

    def test_select_for_mutants_caches_and_counts(self):
        class Fake:
            name, model, calls = "fake", "m1", 0

            def complete(self, prompt):
                Fake.calls += 1
                return json.dumps({"selected": [U1]})

        m = Mutant("M1", "sut/a.py", 3, 3, 0, 1, "CRP", "0", "1")
        cat = [(t, t.split(".")[1], "d") for t in self.KNOWN]
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(2):
                rec = llm.select_for_mutants(Fake(), [m], {"M1": "diff"}, cat, Path(tmp), log=lambda *_: None)
        self.assertEqual(Fake.calls, 1, "second run must come from cache")
        self.assertEqual(rec["M1"]["selected"], [U1])

    def test_unparseable_cached_answer_is_requested_again(self):
        class Fake:
            name, model, calls = "fake2", "m1", 0

            def complete(self, prompt):
                Fake.calls += 1
                return '{"selected": [1, 2, 3' if Fake.calls == 1 else '{"selected": [1]}'

        m = Mutant("M1", "sut/a.py", 3, 3, 0, 1, "CRP", "0", "1")
        cat = [(t, t.split(".")[1], "d") for t in self.KNOWN]
        with tempfile.TemporaryDirectory() as tmp:
            first = llm.select_for_mutants(Fake(), [m], {"M1": "diff"}, cat, Path(tmp), log=lambda *_: None)
            second = llm.select_for_mutants(Fake(), [m], {"M1": "diff"}, cat, Path(tmp), log=lambda *_: None)
            third = llm.select_for_mutants(Fake(), [m], {"M1": "diff"}, cat, Path(tmp), log=lambda *_: None)
        self.assertFalse(first["M1"]["parse_ok"])
        self.assertEqual(second["M1"]["selected"], [U1])
        self.assertEqual(Fake.calls, 2, "a good answer is cached; only the cut-off one is re-asked")
        self.assertEqual(third["M1"]["selected"], [U1])

    def test_repeated_backend_errors_abort(self):
        class Broken:
            name, model = "broken", "x"

            def complete(self, prompt):
                raise ConnectionError("refused")

        ms = [Mutant(f"M{i}", "sut/a.py", 1, 1, 0, 1, "CRP", "0", "1") for i in range(5)]
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            llm.select_for_mutants(Broken(), ms, {m.id: "d" for m in ms}, [], Path(tmp), log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
