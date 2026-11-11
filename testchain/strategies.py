"""Regression test selection (RTS) strategies.

Every strategy answers the same question a CI system faces on each commit:
"given this change, which tests do I run, and in what order?"

All strategies return tests in test-chain order (unit -> integration -> e2e),
because running cheap, fast stages first is how real pipelines get early
feedback. Selection decides *which* tests; chain order decides *when*.
"""

import json
import random
from pathlib import Path

from testchain import cpg
from testchain.kb import KnowledgeBase
from testchain.runner import STAGES, module_of

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Written by bazel/export_affected.py from the BUILD files (bazel query rdeps).
BAZEL_AFFECTED = PROJECT_ROOT / "bazel" / "affected_tests.json"


class Context:
    """Everything a strategy may look at, built from the baseline."""

    def __init__(self, base, source_dir="sut"):
        self.base = base
        self.source_dir = source_dir
        self.tests = base["tests"]
        self.stage = base["stages"]
        self.position = {t: i for i, t in enumerate(self.tests)}
        # line coverage inverted: (file, line) -> set(tests)
        self.covered_by = {}
        for t, files in base["coverage"].items():
            for f, lines in files.items():
                for ln in lines:
                    self.covered_by.setdefault((f, ln), set()).add(t)
        self.imports = base["imports"]
        self.bazel_affected = (json.loads(BAZEL_AFFECTED.read_text())
                               if BAZEL_AFFECTED.exists() else {})
        self._graphs, self._kb, self._links = {}, None, None

    # Static analysis is built lazily from the unchanged source, once per run.
    def kb(self):
        if self._kb is None:
            self._kb = KnowledgeBase.load()
        return self._kb

    def graph(self, with_kb):
        if with_kb not in self._graphs:
            idx = cpg.build(PROJECT_ROOT, kb=self.kb() if with_kb else None)
            idx.index_tests(self.tests)
            self._graphs[with_kb] = idx
        return self._graphs[with_kb]

    def test_components(self):
        if self._links is None:
            self._links = self.kb().test_components(PROJECT_ROOT)
        return self._links

    def ordered(self, selected):
        return sorted(set(selected) & set(self.tests), key=self.position.__getitem__)

    @staticmethod
    def module_name(rel_file):
        return rel_file[:-3].replace("/", ".") if rel_file.endswith(".py") else rel_file


def run_all(ctx, mutant):
    return list(ctx.tests)


def unit_only(ctx, mutant):
    """Gate on the cheapest stage only: the extreme 'shift-left' bet."""
    return [t for t in ctx.tests if ctx.stage[t] == "unit"]


def skip_e2e(ctx, mutant):
    """Unit + integration on every commit; e2e deferred to a nightly run."""
    return [t for t in ctx.tests if ctx.stage[t] in ("unit", "integration")]


def module_map(ctx, mutant):
    """Static dependency selection: tests whose module imports the changed module.

    Cheap and needs no instrumentation, but blind to indirect dependencies
    and to tests that only talk to the system over the network."""
    changed = ctx.module_name(mutant["file"])
    return ctx.ordered(t for t in ctx.tests if changed in ctx.imports.get(module_of(t), []))


def coverage_based(ctx, mutant):
    """Dynamic selection: tests that executed any changed line in the baseline.

    The classic safe-ish RTS technique. Its blind spot: code executed in another
    process (our e2e server) is invisible to the tracer."""
    hit = set()
    for ln in range(mutant["lineno"], mutant["end_lineno"] + 1):
        hit |= ctx.covered_by.get((mutant["file"], ln), set())
    return ctx.ordered(hit)


def file_coverage(ctx, mutant):
    """Dynamic, file-granularity selection (the idea behind Ekstazi): tests that
    executed *any* line of the changed file. Coarser than line coverage, so it
    selects more, but it survives changes to import-time code (module constants,
    dataclass defaults) that line coverage can never attribute to a test."""
    return ctx.ordered(t for t, files in ctx.base["coverage"].items() if mutant["file"] in files)


def coverage_plus_e2e(ctx, mutant):
    """Coverage selection, plus all e2e tests whenever the change touches code
    the e2e stage depends on (here: everything). A pragmatic fix for the
    cross-process blind spot, at the cost of always paying for e2e."""
    return ctx.ordered(set(coverage_based(ctx, mutant)) | {t for t in ctx.tests if ctx.stage[t] == "e2e"})


def bazel_graph(ctx, mutant):
    """Build-graph selection, as `bazel test //...` does it with a warm cache:
    rerun every test *target* (a whole test file) that depends on the changed
    file, directly or transitively, through the dependencies declared in the
    BUILD files. Unlike module_map it follows indirect imports, and it sees
    the e2e stage's dependency on the server because that one is declared by
    hand. Its granularity is a file, not a line, so it cannot skip e2e when
    the e2e target depends on everything."""
    targets = ctx.bazel_affected.get(mutant["file"], [])
    modules = {t[2:].replace("/", ".").replace(":", ".") for t in targets}
    return ctx.ordered(t for t in ctx.tests if module_of(t) in modules)


# --- Static analysis + architecture knowledge (testchain/cpg.py, testchain/kb.py) ---

def component_level(ctx, mutant):
    """Application Component (AC) level selection from the architecture model alone:
    find the component that owns the changed file, add every component that depends
    on it, and run every test linked to any of them. Tests are linked to components
    through the artifacts they import and the processes they start. Coarse but
    cheap, and needs no code analysis."""
    kb = ctx.kb()
    comp = kb.component_of_file.get(mutant["file"])
    if comp is None:
        return list(ctx.tests)
    affected = kb.dependents(comp)
    links = ctx.test_components()
    return ctx.ordered(t for t in ctx.tests if links.get(module_of(t), set()) & affected)


def _reaching(ctx, mutant, with_kb):
    idx = ctx.graph(with_kb)
    return idx.tests_reaching(idx.nodes_for(mutant["file"], mutant["lineno"], mutant["end_lineno"]))


def cpg_static(ctx, mutant):
    """Code property graph only: tests from which the changed code is reachable
    through calls, references and reads, statically and within one process. Like
    coverage it cannot see into the server process, but it needs no test run."""
    return ctx.ordered(_reaching(ctx, mutant, with_kb=False))


def cpg_kb(ctx, mutant):
    """Code property graph plus the knowledge base: the graph also follows the e2e
    test into the server process (process launch, HTTP framework callbacks) and maps
    each request to the API route it exercises. Every test that can reach the change
    is selected, so it is the safe variant."""
    return ctx.ordered(_reaching(ctx, mutant, with_kb=True))


def cpg_kb_gap(ctx, mutant):
    """Escalate to e2e only when static analysis finds no cheaper test that reaches
    the change. Fully automatic, but blind to code that an in-process test reaches
    without exercising the behaviour that changed."""
    selected = _reaching(ctx, mutant, with_kb=True)
    in_process = {t for t in selected if ctx.stage[t] != "e2e"}
    return ctx.ordered(in_process or selected)


def cpg_kb_boundary(ctx, mutant):
    """In-process tests from the graph; e2e tests only when the change is in a
    component the knowledge base marks as the system boundary, and then only the
    e2e tests whose requests can reach the change. Combines AC-level knowledge
    (when e2e is needed) with the graph (which e2e tests)."""
    selected = _reaching(ctx, mutant, with_kb=True)
    if ctx.kb().component_of_file.get(mutant["file"]) in ctx.kb().boundary:
        return ctx.ordered(selected)
    return ctx.ordered(t for t in selected if ctx.stage[t] != "e2e")


def coverage_kb_boundary(ctx, mutant):
    """Line coverage for in-process tests, plus the boundary rule above for e2e.
    Dynamic data where it is precise, static analysis where it is blind."""
    extra = set()
    if ctx.kb().component_of_file.get(mutant["file"]) in ctx.kb().boundary:
        extra = {t for t in _reaching(ctx, mutant, with_kb=True) if ctx.stage[t] == "e2e"}
    return ctx.ordered(set(coverage_based(ctx, mutant)) | extra)


def random_matched(ctx, mutant, size, seed):
    """Random baseline with the same number of tests as a reference strategy.
    If a smart strategy can't beat this, its 'intelligence' is only its size."""
    r = random.Random(seed)
    return ctx.ordered(r.sample(ctx.tests, min(size, len(ctx.tests))))


STATIC_STRATEGIES = {
    "run_all": run_all,
    "unit_only": unit_only,
    "skip_e2e": skip_e2e,
    "module_map": module_map,
    "coverage": coverage_based,
    "file_coverage": file_coverage,
    "coverage+e2e": coverage_plus_e2e,
}
if BAZEL_AFFECTED.exists():
    STATIC_STRATEGIES["bazel_graph"] = bazel_graph
STATIC_STRATEGIES.update({
    "component(kb)": component_level,
    "cpg": cpg_static,
    "cpg+kb": cpg_kb,
    "cpg+kb:gap": cpg_kb_gap,
    "cpg+kb:boundary": cpg_kb_boundary,
    "coverage+kb:boundary": coverage_kb_boundary,
})
