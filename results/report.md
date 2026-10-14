# Test-chain experiment report

Generated from baseline of 2026-10-14T20:53:28 (Python 3.14.4, linux).

## 1. Setup

| Stage | Tests | Median time incl. fixtures (s) |
|---|---:|---:|
| unit | 55 | 0.013 |
| integration | 11 | 0.012 |
| e2e | 6 | 1.023 |

Full chain: **1.05s**. Flaky tests in 3 baseline runs: **0**. Mutants generated: **254**, detectable by the full suite: **207**.

## 2. Test adequacy (mutation score)

| File | Killed | Survived | Timeout | Mutation score |
|---|---:|---:|---:|---:|
| `sut/autoscaler.py` | 22 | 4 | 0 | 84.6% |
| `sut/handlers.py` | 20 | 1 | 0 | 95.2% |
| `sut/ratelimit.py` | 26 | 3 | 0 | 89.7% |
| `sut/registry.py` | 53 | 7 | 0 | 88.3% |
| `sut/router.py` | 38 | 10 | 0 | 79.2% |
| `sut/server.py` | 48 | 21 | 1 | 70.0% |
| **total** | 207 | 46 | 1 | **81.9%** |

Timeouts count as killed (the CI job would time out). Survivors are either test gaps or equivalent mutants (changes with no observable effect); see section 6.

## 3. Where regressions are caught (shift-left profile)

Earliest stage that detects each detectable mutant:

| First detecting stage | Mutants | Share |
|---|---:|---:|
| unit | 147 | 71.0% |
| integration | 41 | 19.8% |
| e2e | 19 | 9.2% |

**19** mutants are caught *only* by e2e, the most expensive stage. Each is a candidate for a new unit/integration test (shift-left opportunity).

## 4. Selection strategies

| Strategy | Detected | Rate (95% CI) | Time vs. all | Mean tests | Median feedback (ms) |
|---|---:|---|---:|---:|---:|
| run_all | 207/207 | 100.0% (98.2%–100.0%) | 100.0% | 72.0 | 10.6 |
| unit_only | 147/207 | 71.0% (64.5%–76.8%) | 1.3% | 55.0 | 9.4 |
| skip_e2e | 188/207 | 90.8% (86.1%–94.0%) | 2.4% | 66.0 | 10.3 |
| module_map | 188/207 | 90.8% (86.1%–94.0%) | 1.3% | 23.5 | 1.0 |
| coverage | 180/207 | 87.0% (81.7%–90.9%) | 0.8% | 10.1 | 0.2 |
| file_coverage | 188/207 | 90.8% (86.1%–94.0%) | 1.3% | 23.6 | 1.0 |
| coverage+e2e | 201/207 | 97.1% (93.8%–98.7%) | 98.3% | 16.1 | 0.3 |
| random(size=coverage) | 74.4/207 | 36.0% (31.9%–39.6%) | 22.5% | 10.2 | 3.1 |

Random baseline picks as many tests as coverage selection, at random; its interval is the min–max over seeds. LLM rows include the model's response latency in time and feedback.

### Paired comparison against coverage-based selection (exact McNemar, Holm-corrected)

| Strategy | Caught only by it | Caught only by coverage | p (Holm) | Mean time saved vs. all (ms, 95% CI) |
|---|---:|---:|---:|---|
| unit_only | 8 | 41 | 7.86e-06 | 1034.8 – 1034.8 |
| skip_e2e | 8 | 0 | 0.0234 | 1022.7 – 1022.7 |
| module_map | 8 | 0 | 0.0234 | 1034.0 – 1035.2 |
| file_coverage | 8 | 0 | 0.0234 | 1033.6 – 1034.5 |
| coverage+e2e | 21 | 0 | 4.77e-06 | 16.6 – 18.3 |

### LLM run diagnostics

- **ollama-qwen2.5-coder-3b**: mutants covered = 40/207, mean latency s = 107.65, parse failures = 0, errors = 0, responses with hallucinated ids = 4, hallucinated ids total = 4, empty selections = 1, most common selection share = 0.15

### Sampled LLM run `ollama-qwen2.5-coder-3b`: all strategies on the same 40 mutants

| Strategy | Detected | Rate (95% CI) | Time vs. all | Mean tests |
|---|---:|---|---:|---:|
| run_all | 40/40 | 100.0% (91.2%–100.0%) | 100.0% | 72.0 |
| unit_only | 30/40 | 75.0% (59.8%–85.8%) | 1.3% | 55.0 |
| skip_e2e | 36/40 | 90.0% (76.9%–96.0%) | 2.4% | 66.0 |
| module_map | 36/40 | 90.0% (76.9%–96.0%) | 1.3% | 22.9 |
| coverage | 33/40 | 82.5% (68.1%–91.3%) | 0.7% | 9.8 |
| file_coverage | 36/40 | 90.0% (76.9%–96.0%) | 1.4% | 23.6 |
| coverage+e2e | 38/40 | 95.0% (83.5%–98.6%) | 98.3% | 15.8 |
| llm:ollama-qwen2.5-coder-3b | 38/40 | 95.0% (83.5%–98.6%) | 10357.6% | 33.3 |
| llm+file_coverage:ollama-qwen2.5-coder-3b | 40/40 | 100.0% (91.2%–100.0%) | 10358.0% | 40.2 |

## 5. Return on investment model

Measured times are scaled so the full chain takes **60 min**. Assumptions (edit `roi.json`): 10% of commits carry a regression; an escaped regression costs **480 engineer-minutes**; a CI minute is worth 0.05 engineer-minutes; 25% of developer waiting time is lost. All costs below are engineer-minutes per commit.

| Strategy | Expected cost | Saving vs. all | Break-even escape cost | @60 | @240 | @480 | @960 | @1920 | @3840 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| run_all | 16.58 | +0.00 | – | 16.58 | 16.58 | 16.58 | 16.58 | 16.58 | 16.58 |
| unit_only | 14.14 | +2.44 | 564 | 1.96 | 7.18 | 14.14 | 28.05 | 55.88 | 111.53 |
| skip_e2e | 4.82 | +11.76 | 1,761 | 0.97 | 2.62 | 4.82 | 9.23 | 18.04 | 35.66 |
| module_map | 4.62 | +11.96 | 1,783 | 0.77 | 2.42 | 4.62 | 9.03 | 17.84 | 35.46 |
| coverage | 6.39 | +10.19 | 1,261 | 0.91 | 3.26 | 6.39 | 12.65 | 25.17 | 50.21 |
| file_coverage | 4.63 | +11.95 | 1,782 | 0.78 | 2.43 | 4.63 | 9.04 | 17.85 | 35.47 |
| coverage+e2e | 17.73 | -1.15 | 84 | 16.51 | 17.03 | 17.73 | 19.12 | 21.90 | 27.47 |
| random(size=coverage) | 34.56 | -17.98 | 199 | 7.66 | 19.19 | 34.56 | 65.30 | 126.78 | 249.74 |

*Break-even escape cost*: if a regression that slips past selection costs more than this, running everything is cheaper. It is the single number to argue about with a team.

## 6. Surviving mutants (test gaps or equivalent mutants)

<details><summary>46 survivors</summary>

**M0004** `sut/autoscaler.py:17` ROR: `in_flight <= 0` → `in_flight < 0`  
**M0015** `sut/autoscaler.py:35` ROR: `desired < current` → `desired <= current`  
**M0022** `sut/autoscaler.py:36` CRP: `1` → `2`  
**M0023** `sut/autoscaler.py:36` AOR: `current // 2` → `current / 2`  
**M0043** `sut/handlers.py:45` CRP: `3` → `4`  
**M0057** `sut/ratelimit.py:21` ROR: `elapsed > 0` → `elapsed >= 0`  
**M0059** `sut/ratelimit.py:31` RVR: `False` → `None`  
**M0065** `sut/ratelimit.py:42` ROR: `missing <= 0` → `missing < 0`  
**M0080** `sut/registry.py:13` CRP: `300` → `301`  
**M0081** `sut/registry.py:14` CRP: `50` → `51`  
**M0085** `sut/registry.py:37` CRP: `30` → `31`  
**M0087** `sut/registry.py:39` CRP: `5` → `6`  
**M0088** `sut/registry.py:40` CRP: `10` → `11`  
**M0105** `sut/registry.py:31` CRP: `True` → `False`  
**M0116** `sut/registry.py:88` ROR: `spec.max_replicas > MAX_REPLICAS_LIMIT` → `spec.max_replicas >= MAX_REPLICAS_LIMIT`  
**M0138** `sut/router.py:17` CRP: `0` → `1`  
**M0139** `sut/router.py:25` CRP: `False` → `True`  
**M0146** `sut/router.py:119` ASR: `lease.replica.served += 1` → `lease.replica.served -= 1`  
**M0150** `sut/router.py:65` ROR: `len(pool) < target` → `len(pool) <= target`  
**M0156** `sut/router.py:105` ROR: `target > len(pool)` → `target >= len(pool)`  
**M0161** `sut/router.py:119` CRP: `1` → `2`  
**M0163** `sut/router.py:54` CRP: `1` → `2`  
**M0176** `sut/router.py:104` CRP: `1` → `2`  
**M0178** `sut/router.py:51` CRP: `50` → `51`  
**M0179** `sut/router.py:51` CRP: `100` → `101`  
**M0187** `sut/server.py:140` RVR: `0` → `None`  
**M0188** `sut/server.py:18` CRP: `64` → `65`  
**M0189** `sut/server.py:18` CRP: `1024` → `1025`  
**M0190** `sut/server.py:140` CRP: `0` → `1`  
**M0191** `sut/server.py:24` CRP: `50` → `51`  
**M0192** `sut/server.py:24` CRP: `100` → `101`  
**M0198** `sut/server.py:58` RVR: `405, {"error": "method not allowed"}, {}` → `None`  
**M0199** `sut/server.py:60` LCR: `m and method == "POST"` → `(m or method == "POST")`  
**M0201** `sut/server.py:77` ROR: `payload is not None` → `payload is None`  
**M0202** `sut/server.py:87` ROR: `length > MAX_BODY_BYTES` → `length >= MAX_BODY_BYTES`  
**M0204** `sut/server.py:127` CRP: `8080` → `8081`  
**M0205** `sut/server.py:129` CRP: `100` → `101`  
**M0224** `sut/server.py:88` ROR: `length <= 16 * MAX_BODY_BYTES` → `length < 16 * MAX_BODY_BYTES`  
**M0225** `sut/server.py:90` CRP: `True` → `False`  
**M0231** `sut/server.py:58` CRP: `405` → `406`  
**M0232** `sut/server.py:62` LCR: `res.replica or ""` → `(res.replica and "")`  
**M0236** `sut/server.py:88` AOR: `16 * MAX_BODY_BYTES` → `16 / MAX_BODY_BYTES`  
**M0239** `sut/server.py:36` CRP: `200` → `201`  
**M0241** `sut/server.py:47` CRP: `200` → `201`  
**M0246** `sut/server.py:88` CRP: `16` → `17`  
**M0254** `sut/server.py:64` AOR: `res.retry_after + 0.999` → `res.retry_after - 0.999`  

</details>

## 7. Test value

Tests with the most *unique* kills (removing them loses detection):

| Test | Stage | Kills | Unique kills | ms |
|---|---|---:|---:|---:|
| `test_handlers.TestHandlers.test_sum` | unit | 7 | 5 | 0.06 |
| `test_registry.TestValidate.test_memory_bounds_are_inclusive` | unit | 15 | 5 | 0.10 |
| `test_api.TestFunctionLifecycleE2E.test_03_error_mapping` | e2e | 66 | 5 | 16.20 |
| `test_registry.TestValidate.test_timeout_bounds` | unit | 15 | 4 | 0.09 |
| `test_router.TestInvoke.test_saturated_function_returns_503` | unit | 45 | 4 | 0.18 |
| `test_gateway_flow.TestAppHandle.test_unknown_route_and_method` | integration | 9 | 4 | 0.11 |
| `test_autoscaler.TestNeededReplicas.test_rounds_up` | unit | 5 | 3 | 0.05 |
| `test_handlers.TestHandlers.test_sum_rejects_bad_input` | unit | 5 | 3 | 0.50 |

1 tests kill no mutant at all: `test_registry.TestSpecFromDict.test_rejects_non_object`

## 8. Threats to validity

- **Mutants are not real regressions.** They are small, single-point faults; real commits are larger and touch several files. Mutation-based ground truth is standard in RTS research but should be complemented with real fault data (e.g. historical CI failures).
- **Toy scale.** One small service, ~70 tests. Absolute times are meaningless; ratios and the break-even analysis are the transferable results.
- **Equivalent mutants** inflate the survivor count and were not manually filtered.
- **Timing noise.** Durations are medians of repeated runs on one machine; class fixture cost is attributed to the first test of the class.
- **LLM non-determinism.** Temperature 0 and cached responses make one run reproducible, not representative; repeat with several models/seeds before generalising.
