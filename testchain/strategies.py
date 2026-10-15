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

from testchain.runner import STAGES, module_of

# Written by bazel/export_affected.py from the BUILD files (bazel query rdeps).
BAZEL_AFFECTED = Path(__file__).resolve().parents[1] / "bazel" / "affected_tests.json"


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
