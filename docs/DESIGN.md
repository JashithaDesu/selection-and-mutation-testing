# Design notes

Why things are built the way they are. If you are asked "why did you…?" in an
interview, the answer should be here.

## The research question

> Given a commit, which tests should run, in which order, and is the time saved worth the
> regressions that slip through?

Three sub-questions, each answered by a section of the report:

1. **Adequacy**: how good is the test suite at all? (mutation score, surviving mutants)
2. **Placement**: at which stage of the chain is each bug first caught? (shift-left profile)
3. **Selection**: how much detection does each strategy keep, at what cost? (strategies, McNemar, ROI)

## Why mutants as ground truth

To evaluate a selection strategy you need commits where you *know* there is a bug and *know*
which tests catch it. Real CI history has this, but you rarely have access to it as a student.
Mutation testing manufactures it: every mutant is a bug, and running the whole suite against
it shows which tests detect it. This is the standard method in regression test selection
research (see e.g. Gligoric et al., Ekstazi, ISSTA 2015; Just et al., "Are mutants a valid
substitute for real faults", FSE 2014). That second paper is also the main threat to validity.
Mutants correlate with real faults, but they are smaller and simpler.

**Why write a mutation engine instead of using mutmut?** Three reasons. PyPI was not an option
in the build environment. mutmut 3 does not support native Windows. And owning the engine
makes it possible to produce one-token diffs that look like real commits, which matters
because the LLM reads those diffs.

Operators: AOR, ROR, LCR, UOD, CRP, RVR, ASR (Offutt/Ammann naming). Skipped on purpose:
docstrings, f-string internals, the `__main__` block, lines marked `# pragma: no mutate`.

## Why the kill matrix is computed once

Every strategy is evaluated *offline* against the same matrix (mutant × test → fails?).
That makes strategy comparisons **paired**: same bugs, different selections. This is why
McNemar's test is the right test (paired binary outcomes), not a chi-square on two rates.
It also means a new strategy costs milliseconds to evaluate, not another 90-second run.

## Why flakiness is checked before anything else

If a test fails randomly, a mutant can look "killed" by noise. The baseline runs the suite
three times and refuses to continue if any test's outcome changes. Property tests use a
fixed seed (`tests/support.py`) for the same reason.

## The cost model

- A selection's cost = its tests' median durations **plus fixture cost of every class touched**.
  Starting the e2e server is paid once per class, and only if one of its tests is selected.
  A naive "sum of test durations" model would make e2e look nearly free.
- **Feedback time** = time until the first failing test finishes, running in chain order.
  This is what a developer actually waits for on a bad commit.
- LLM strategies pay the model's response latency on top. A selector that thinks for 20 s to
  save 2 s is a loss, and the model has to show that.

## The ROI model

```
cost per commit = ci_weight · test_minutes
                + dev_weight · (p · wait_if_buggy + (1 - p) · test_minutes)
                + p · miss_rate · escape_cost
```

The measured times are scaled so the full chain takes `full_chain_minutes`. Then
we can talk about an industrial pipeline without pretending a 0.5 s toy suite is one.
Every parameter is an assumption in `roi.json`, to be replaced with an organisation's
data. The **break-even escape cost** is the robust output: it does not depend on
`escape_cost`, and it turns "should we skip tests?" into "do our escaped bugs cost more
than X engineer-minutes?"

## Strategies, and what each one tests

| Strategy | Hypothesis it represents |
|---|---|
| `run_all` | The safe default; reference point |
| `unit_only` | Extreme shift-left: is the unit stage alone enough? |
| `skip_e2e` | Common practice: e2e only nightly |
| `module_map` | Static dependencies (no instrumentation needed) |
| `coverage` | Classic dynamic RTS at line granularity |
| `file_coverage` | Dynamic RTS at file granularity (the Ekstazi idea) |
| `coverage+e2e` | Patch for coverage's cross-process blind spot |
| `random(size=coverage)` | Control: is "smart" selection just "small" selection? |
| `llm:<model>` | Can a model that reads the diff select tests? |
| `llm+file_coverage` | Model adds what static analysis cannot see (e.g. "this needs e2e") |

## The LLM setup and its traps

- The model sees exactly what a CI bot would see: the diff and a test catalogue. It never
  sees the kill matrix.
- **Hallucinated test ids are counted, never auto-corrected.** Only an unambiguous id suffix is
  accepted (`TestX.test_y` when exactly one test matches). A silently "repaired" answer would
  overstate the model.
- Temperature 0, fixed seed, cached responses: one run is reproducible. That does not make it
  representative, so repeat across models.
- Main risk to the result: mutant diffs are tiny and the bug is often visible in the diff.
  Real pull requests are bigger and noisier. Treat LLM results as an upper bound.

## Known limitations

- One small Python service. The ratios transfer; the absolute times do not.
- Equivalent mutants are not removed (that needs manual review of the 46 survivors).
- Timeout mutants (1) are counted as detected in the mutation score but excluded from
  the selection comparison, since no single test can be credited with them.
- Durations come from one machine. Run the baseline on the machine you report from.

## Natural next steps

1. Run the LLM strategy with 2–3 models and compare against `file_coverage` on the same mutants.
2. **Shift-left experiment**: write unit tests that kill the 19 e2e-only mutants, re-run, and
   measure how the ceiling and the break-even move. That is a before/after ROI study.
3. Replace mutants with real history: replay real failing CI runs from an open-source
   project through the same evaluator (the kill matrix format is the only interface).
4. Port the harness to a Go codebase (for example a Kubernetes controller) to test whether the findings hold across languages.
