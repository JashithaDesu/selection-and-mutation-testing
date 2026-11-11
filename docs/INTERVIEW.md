# Interview notes

Questions you are likely to get about this project, with short answers grounded in
the actual results. Read `DESIGN.md` for the longer reasoning.

## The 30-second pitch

> CI pipelines spend most of their time on slow tests. I built a lab to measure which tests
> a pipeline can skip on a given commit, and what skipping costs. I injected 254 artificial
> bugs into a small FaaS gateway, recorded which of 72 tests catch each one, and compared
> eight selection strategies. Coverage-based selection kept 87% of detection at under 1% of
> the test time. Every strategy that skips e2e hits a 91% ceiling, because 9% of bugs are
> only visible end to end. That ceiling is where I test whether an LLM reading the diff can
> decide when e2e is needed.

## Likely questions

**What is mutation testing?**
Changing the code on purpose (a `<` becomes `<=`, a `64` becomes `65`) and checking
whether any test fails. A failing test "kills" the mutant. The share of mutants killed is
the mutation score: a better measure of test quality than line coverage, because coverage only
says code *ran*, not that anything *checked* the result.

**Why mutants instead of real bugs?**
I needed bugs where I know the answer, meaning which tests catch them. Real CI history has that, but
I had no access to it. Mutants are the standard substitute in regression test selection research. Their weakness: they
are smaller and simpler than real bugs (Just et al., FSE 2014). That is my main threat to validity.

**Your mutation score is 81.9%. Are your tests bad?**
Not all survivors are gaps. Three kinds, each with an example:
- *Equivalent mutant* (M0004): `in_flight <= 0` → `< 0`. At 0 the code falls through to
  `ceil(0/n)`, which is also 0. Same behaviour, nothing to catch.
- *Test mirrors the code* (M0080): `MAX_TIMEOUT_S` 300 → 301 survives because the test
  computes its expectation from the same constant. Fix: hardcode 300/301 in the test.
- *Real gap* (M0182): no test checked that `invoke()` returns 503 when every replica is busy.
  I added `test_saturated_function_returns_503`; it killed 4 survivors (50 → 46), two of them
  validation mutants it caught only because it uses the boundary value `target_concurrency=1`.

**What is the kill matrix and why compute it once?**
A table: mutant × test → did it fail. Every strategy is replayed offline against the same
table, so comparisons are *paired* (same bugs, different selections). That is why I use
McNemar's test instead of comparing two percentages independently.

**Why did line-coverage selection miss bugs that cheaper strategies caught?**
Two blind spots, both measured:
1. Cross-process code: e2e tests start the server as a separate process, so the coverage
   tracer sees nothing they execute.
2. Import-time code: module constants and dataclass defaults run when the module is
   imported, before any test starts, so no test is recorded as covering them.
File-level selection (the Ekstazi approach) fixes #2 by selecting more tests (2.4x as many).

**What does the random baseline prove?**
That selection logic matters, not just selection size. Random selection with the same number of tests as coverage
catches 36.0% of bugs vs 87.0%.

**How did you avoid flaky results?**
The baseline runs the suite three times and refuses to continue if any test's outcome
changes. Property tests use a fixed seed. Two independent full runs gave identical kill matrices.

**How does the ROI model work, and isn't it just made-up numbers?**
The parameters (defect rate, cost of an escaped bug, value of CI minutes) are assumptions,
and I say so. The robust output is the *break-even escape cost*, which does not depend on the
escape-cost guess: skipping e2e is cheaper unless an escaped bug costs more than about 1,770
engineer-minutes. That turns "should we skip tests?" into one question a team can answer from
its own incident data.

**How is the LLM evaluated fairly?**
It sees only what a CI bot would see: the diff and a list of test names. It never sees the kill matrix. Invented
test names are counted as hallucinations, not fixed. Its response time is added to the cost, so a slow model has to
earn its time. Responses are cached at temperature 0, so a run can be reproduced.
Limitation: mutant diffs are tiny, so the bug is often obvious from the diff. Real PRs are harder.
Result (Qwen2.5-Coder 3B, 40 sampled bugs): 95% detection vs 82.5% for coverage on the same bugs,
not significant at n = 40, at ~108 s per decision, about 200x the cost of the whole 0.5 s suite.
It went past the 90% ceiling by picking e2e tests, but also selected 33 tests on average vs 9.
Getting there took three fixes: a timeout, a prompt example the model copied, and cut-off answers.

**Did anything behave differently on another machine?**
Yes. Mutant M0206 changes `print(..., flush=True)` to `flush=False` in the server start-up.
On the build machine it *survived*, because that environment set `PYTHONUNBUFFERED=1`, which hides
the missing flush. On my laptop the e2e test waited forever for the "LISTENING" line and the run
timed out. Two fixes: the harness now removes `PYTHONUNBUFFERED` so every machine behaves like
production, and the e2e helper gives up after 3 s instead of hanging, so the bug is now *caught*
in about 6 s. Lesson: a test environment that differs from production can hide real bugs, and
tests need their own timeouts.

**Did the LLM evaluation work the first time?**
No. In the first run, 32 of 40 answers were exactly the example list `[3, 17, 42]` from my
prompt. The 3B model copied the example instead of reading the diff. I removed concrete numbers from
the prompt, re-ran, and added an automatic validity check: if over half the answers are the
same list, the report flags the run as invalid. Lesson: an LLM evaluation needs its own sanity
checks, just like a test suite.

**What would you do next / in the thesis?**
1. Shift-left study: write unit tests for the 19 e2e-only bugs and measure how the ceiling
   and break-even move.
2. Replace mutants with real failing CI runs from an open-source project.
3. Compare several LLMs, and LLM + file coverage, on the same bugs.

**Why no pytest / mutmut?**
Standard library only, so it runs anywhere with no install, including native Windows (mutmut 3
does not). Tests are plain `unittest` and also run under pytest.

## The code property graph extension

**In one sentence?**
The cheap in-process strategies all stop at 91% because 19 bugs only show end to end; a code graph
plus an architecture model decides when a change needs the e2e stage and which e2e tests, and
catches all 207 bugs at 18% of the test time.

**What is a code property graph?**
One graph that combines a program's syntax tree with how code calls and depends on other code
(Yamaguchi et al., 2014). Mine is lightweight: one node per function, class body, constant or
`__main__` block, edges for calls, references, reads, imports and test fixtures. A change maps to
the node owning the changed line; a test is affected if it can reach that node.

**Why didn't you use Joern?**
The repository runs on the standard library only, and test selection needs reachability between
functions, not control flow inside them. I'd use Joern on a real codebase, and comparing the two
graphs is listed as a next step.

**Why isn't the graph enough on its own?**
Measured: it catches 90.8%, the same as coverage. The e2e test starts the server as a subprocess,
`http.server` calls `do_GET` implicitly, and `_dispatch` forwards every request to one function.
None of that is a call edge in the code. The knowledge base adds exactly those three links.

**What does the knowledge base contain, and how do you stop it going stale?**
Components and their files, dependencies, which component is the external boundary, and how the
HTTP interface and the server process connect. Routes and test-to-route links are mined from code.
What is written by hand is checked against the imports by `python -m testchain kb`, and a test
fails the build on drift.

**Isn't 100% suspicious?**
Yes, so say why: every e2e-only bug in this system lives in the boundary component, which is the
ideal case for the boundary rule. A bug deeper in the stack that only shows end to end would escape
it. The safe variant (`cpg+kb`) would still catch it, at 86% of the cost.

**What went wrong along the way?**
The first version missed 2 bugs. One was `len(registry)` calling `__len__` implicitly, the other a
route condition that runs for every request but was attributed to one route. Both fixes are
general, and each has a test that fails without it.

## Things to be able to do live

- `python -m testchain show M0182`: explain why it survived.
- Open `tests/integration/test_gateway_flow.py` and explain `test_cold_warm_idle_cold` and the fake clock.
- Explain why `simulate()` in `evaluate.py` charges class fixture time once.
- `python -m testchain kb`: explain the 9 routes and the 55 knowledge-base edges.
- In `testchain/kb.py`, explain `route_regions` and why the `if` line is not part of the route.
- Show that `cpg` selects no e2e test for a change to the `/healthz` route, and `cpg+kb:boundary`
  selects exactly the 2 tests that request it (`testchain_tests/test_cpg.py`).
