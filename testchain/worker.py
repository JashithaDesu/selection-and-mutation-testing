"""Test-suite worker, executed as a separate process inside a project copy.

    python path/to/worker.py --out result.json [--coverage sut]

It is deliberately standalone (imports nothing from `testchain`) so it can run
against a mutated copy of the project without the harness itself being
affected by the mutation.

Output JSON:
  tests:        ordered list of test ids
  outcomes:     {test_id: "pass" | "fail" | "error" | "skip"}
  durations:    {test_id: seconds spent inside the test (setUp..tearDown)}
  class_overhead: {class_id: seconds of setUpClass/tearDownClass + gaps}
  load_errors:  [module names that failed to import]
  coverage:     {test_id: {relative_file: [line, ...]}}   (only with --coverage)
"""

import argparse
import json
import os
import sys
import threading
import time
import unittest


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def class_of(test_id):
    return test_id.rsplit(".", 1)[0]


class LineTracer:
    """Minimal per-test line coverage using sys.settrace (works on any CPython)."""

    def __init__(self, source_dir):
        self.prefix = os.path.abspath(source_dir) + os.sep
        self.root = os.path.abspath(".") + os.sep
        self.current = None

    def _local(self, frame, event, arg):
        if event == "line" and self.current is not None:
            fname = frame.f_code.co_filename
            rel = fname[len(self.root):].replace(os.sep, "/")
            self.current.setdefault(rel, set()).add(frame.f_lineno)
        return self._local

    def _global(self, frame, event, arg):
        if frame.f_code.co_filename.startswith(self.prefix):
            if self.current is not None:
                fname = frame.f_code.co_filename
                rel = fname[len(self.root):].replace(os.sep, "/")
                self.current.setdefault(rel, set()).add(frame.f_lineno)
            return self._local
        return None

    def start(self):
        sys.settrace(self._global)
        threading.settrace(self._global)

    def stop(self):
        sys.settrace(None)
        threading.settrace(None)


class RecordingResult(unittest.TestResult):
    def __init__(self, tracer=None):
        super().__init__()
        self.tracer = tracer
        self.outcomes, self.durations, self.coverage = {}, {}, {}
        self.class_overhead = {}
        self.order = []
        self._t0 = None
        self._last_stop = time.perf_counter()
        self._last_class = None

    def startTest(self, test):
        super().startTest(test)
        now = time.perf_counter()
        tid = test.id()
        cls = class_of(tid)
        # Time between the previous test and this one is fixture work
        # (tearDownClass of the previous class + setUpClass of this one).
        # Charge it to the class whose first test is about to run.
        if cls != self._last_class:
            self.class_overhead[cls] = self.class_overhead.get(cls, 0.0) + (now - self._last_stop)
            self._last_class = cls
        self.order.append(tid)
        self.outcomes[tid] = "pass"
        if self.tracer:
            self.tracer.current = self.coverage.setdefault(tid, {})
        self._t0 = time.perf_counter()

    def stopTest(self, test):
        now = time.perf_counter()
        self.durations[test.id()] = now - self._t0
        if self.tracer:
            self.tracer.current = None
        self._last_stop = now
        super().stopTest(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes[test.id()] = "fail"

    def addError(self, test, err):
        super().addError(test, err)
        tid = test.id() if hasattr(test, "id") else str(test)
        if tid in self.outcomes:
            self.outcomes[tid] = "error"

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes[test.id()] = "skip"

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.outcomes[test.id()] = "fail"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--tests", default="tests")
    ap.add_argument("--coverage", default=None, help="source dir to trace per test")
    args = ap.parse_args(argv)

    sys.path.insert(0, os.path.abspath("."))
    loader = unittest.TestLoader()
    suite = loader.discover(args.tests, top_level_dir=".")

    load_errors, runnable = [], unittest.TestSuite()
    for t in flatten(suite):
        if type(t).__name__ == "_FailedTest":
            load_errors.append(t.id().replace("unittest.loader._FailedTest.", ""))
        else:
            runnable.addTest(t)
    # Keep class fixtures working: re-group consecutive tests of a class.
    tracer = LineTracer(args.coverage) if args.coverage else None
    result = RecordingResult(tracer)
    if tracer:
        tracer.start()
    start = time.perf_counter()
    runnable.run(result)
    end = time.perf_counter()
    if tracer:
        tracer.stop()
    if result._last_class is not None:
        tail = end - result._last_stop  # final tearDownClass
        result.class_overhead[result._last_class] += tail

    # Errors raised in setUpClass are reported against a pseudo test; mark the
    # whole class as errored so the kill matrix stays per real test.
    for test, _ in result.errors:
        tid = test.id() if hasattr(test, "id") else str(test)
        if tid.startswith("setUpClass (") or tid.startswith("tearDownClass ("):
            cls = tid[tid.index("(") + 1:tid.rindex(")")]
            for t in flatten(suite):
                if class_of(t.id()) == cls:
                    result.outcomes[t.id()] = "error"
                    if t.id() not in result.order:
                        result.order.append(t.id())
                        result.durations[t.id()] = 0.0

    out = {
        "tests": result.order,
        "outcomes": result.outcomes,
        "durations": result.durations,
        "class_overhead": result.class_overhead,
        "load_errors": load_errors,
        "wall_time": end - start,
    }
    if tracer:
        out["coverage"] = {
            tid: {f: sorted(lines) for f, lines in cov.items()} for tid, cov in result.coverage.items()
        }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
