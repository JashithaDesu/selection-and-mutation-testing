# selection-and-mutation-testing

**Which tests should a CI pipeline run on a given commit, and what does skipping the rest cost?**

This repository is a small, fully reproducible lab for that question. It contains:

1. **A system under test**: a mini Function-as-a-Service gateway (function registry,
   token-bucket rate limiter, concurrency autoscaler with scale-to-zero, router, HTTP API).
2. **A staged test chain**: 55 unit, 11 integration and 6 end-to-end tests, gated in CI
   so cheap stages run first (`.github/workflows/ci.yml`).
3. **An experiment harness** (`testchain/`) that
   - injects **254 artificial regressions** with its own AST mutation engine,
   - runs the full chain against every one to build a **kill matrix** (which test catches which bug),
   - replays **test-selection strategies** against that ground truth, including an **LLM** that reads the diff,
   - reports detection rate, cost, time-to-first-failure, significance tests and an **ROI / break-even model**.

Python standard library only. Nothing to install (matplotlib is optional, for the chart).
Runs on Windows, macOS and Linux.

## Run it

```bash
python -m unittest discover -s tests -t .        # the test chain itself (~0.5 s)
python -m testchain all --jobs 4                 # full experiment (~1-2 min)
#   -> results/report.md, results/cost_vs_detection.png, results/*.json

python -m testchain show M0077                   # one injected bug: diff + which tests catch it
```

On Windows use `py` instead of `python`.

### Add an LLM strategy

```bash
ollama pull qwen2.5-coder:7b
python -m testchain llm --print-prompt                       # inspect exactly what the model sees
python -m testchain llm --model qwen2.5-coder:7b --sample 40 # quick, random 40 bugs
python -m testchain llm --model qwen2.5-coder:7b             # all 207 detectable bugs
python -m testchain evaluate
```

`--backend anthropic --model <model>` works too (needs `ANTHROPIC_API_KEY`). Responses are cached
in `results/llm_cache/`, so re-running `evaluate` is free and a run is reproducible.

## Results (current baseline)

| Strategy | Bugs caught before merge | Test time vs. running everything |
|---|---:|---:|
| run everything | 100% (207/207) | 100% |
| unit tests only | 71.0% | 0.9% |
| line-coverage selection | 87.0% | 0.5% |
| file-coverage selection (Ekstazi-style) | 90.8% | 0.9% |
| skip e2e | 90.8% | 1.7% |
| random, same size as coverage | 36.0% | 19.4% |

What the numbers say (details and statistics in `results/report.md`):

- **The e2e stage is 98% of the cost and catches only 9.2% of bugs, but nothing else catches those 9.2%.**
  Every in-process strategy hits the same 90.8% ceiling. You get past it either by writing
  cheaper tests for those 19 bugs (shift left), or by knowing *when* a change needs e2e.
  That is the opening for an LLM.
- **Line-coverage selection has two blind spots, measured here:** code running in another process
  (the e2e server is invisible to the tracer), and import-time code (module constants, dataclass
  defaults run before any test starts). File-level selection fixes the second, at about 2.4x the tests.
- **Selection logic is real value:** coverage selection catches 2.4x more bugs than a random selection of the same size.
- **Break-even:** with the default assumptions, skipping e2e stays cheaper than running everything
  until an escaped regression costs more than about 1,770 engineer-minutes. Change `roi.json` to your
  organisation's numbers; the break-even column is the thing to argue about.
- **Mutation score 81.9%.** 46 mutants survive. Some are test gaps; some are equivalent mutants.
  The report lists them all.

## Layout

```
sut/                 system under test (mini FaaS gateway)
tests/unit/          fast, isolated, fake clock, seeded property tests
tests/integration/   real components wired together, in process
tests/e2e/           real server process, HTTP only (black box)
testchain/           experiment harness
  mutate.py          AST mutation engine (7 operators, one-token diffs)
  worker.py          runs the suite in a sandbox copy, per-test timing + line coverage
  runner.py          baseline (flakiness check), parallel kill-matrix runner
  strategies.py      selection strategies
  llm.py             LLM selection (Ollama / Anthropic), strict parsing, caching
  evaluate.py        metrics, cost model, ROI
  stats.py           Wilson CI, exact McNemar, bootstrap, Holm
  report.py          markdown report + chart
testchain_tests/     tests for the harness itself
docs/DESIGN.md       why each decision was made, and threats to validity
```
