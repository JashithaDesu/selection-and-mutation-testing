# Results: five runs of `environments.yml`

Five complete runs of the workflow on GitHub-hosted `ubuntu-24.04` runners (15 October 2026),
after `flake.lock` was committed (nixpkgs pinned to `ac62194c`). Run #1 is excluded: its
devcontainer job failed in a post-processing step (a workflow bug, since fixed), so it has no
complete set of results.

Each environment ran as its own job on a fresh runner. All five runs were started within about
half an hour of each other.

## 1. Functional results: what the experiment found

| environment | Python | functional sha256 | runs with this hash |
|---|---|---|---|
| traditional (runner's system Python) | 3.12.3 | `b6aaa330cafb` | 5 / 5 |
| devcontainer (`python:3.12-slim`) | 3.12.15 | `b6aaa330cafb` | 5 / 5 |
| Nix (`python312`, pinned nixpkgs) | 3.12.12 | `b6aaa330cafb` | 5 / 5 |
| setup-python 3.10 / 3.11 / 3.12 / 3.13 / 3.14 | 3.10.22 … 3.14.8 | `b6aaa330cafb` | 5 / 5 each |
| **setup-python 3.9** | **3.9.25** | **`cb891ea295bd`** | **5 / 5** |

In every environment and every run: 254 mutants; 207 killed, 46 survived, 1 timeout.

**Result:** the experiment is deterministic within a Python version (all 40 jobs on 3.10+ match: 8 environments × 5 runs),
but Python 3.9 produces different experiment data in every run.

### What differs on Python 3.9, and why

Reproduced locally with Python 3.9.25, then compared field by field with 3.12:

| part of the result | 3.9 vs. 3.10+ |
|---|---|
| generated mutants | identical |
| which tests kill each mutant | identical |
| bugs caught by every strategy | identical |
| mean tests selected by `coverage` | 10.063 vs. 10.053 |
| mean tests selected by `coverage+e2e` | 16.063 vs. 16.053 |
| mean tests selected by `random(size=coverage)` | 10.179 vs. 10.169 (its size follows `coverage`) |

- The per-test line coverage differs in exactly one place. On 3.9, test
  `test_gateway_flow.TestAppHandle.test_invalid_spec_raises_for_http_layer` is recorded as
  running `sut/server.py` line 66 (`return 404, {"error": "not found"}, {}`); on 3.10+ it is not.
- That test makes `handle()` raise an exception inside a `with self.lock:` block. On 3.9, the
  tracer reports a line event for the last line of the `with` body while the exception unwinds
  the block, although that line never runs.
- Two mutants are on line 66 (M0193, M0221), so on 3.9 coverage-based selection picks that test
  for both: 2 extra selections over 207 detectable bugs.
- Likely cause: PEP 626 (Python 3.10) made the line numbers reported to tracers precise. Before
  3.10, cleanup code generated for a `with` block could be attributed to the block's last line.
  The effect is measured; the attribution to PEP 626 is the documented behaviour, not separately
  tested here.

**Interpretation:** the same code, the same tests and no third-party dependencies still produce
different experiment data depending only on the interpreter version. All headline numbers
(counts, detection rates) were identical, so the difference is visible only by hashing the full
results. The difference does not change any conclusion of the mutation study: same data quality
on detection, slightly higher selection cost on 3.9.

## 2. Rebuild reproducibility

| check | result in 5 / 5 runs |
|---|---|
| devcontainer: two `--no-cache` image builds have the same image ID | **no** (different every time) |
| Nix: `nix build .#experiment --rebuild` gives byte-identical output | **yes** |

The Docker image is not reproducible even with an unchanged Dockerfile; the Nix sandbox build,
which runs the whole experiment, is. The image-ID difference has not yet been traced to specific
files (next step: compare the two images layer by layer, and pin the base image by digest).

## 3. Timing (5 runs each, mean ± standard deviation)

| environment | run time (s) | range | random-strategy cost (%) | skip_e2e cost (%) |
|---|---|---|---|---|
| devcontainer | 113.5 ± 17.3 | 86.8–126.3 | 22.9 ± 0.4 | 0.8 ± 0.0 |
| nix | 52.3 ± 0.6 | 51.4–53.0 | 18.8 ± 0.1 | 1.6 ± 0.2 |
| traditional | 47.9 ± 1.8 | 45.0–49.7 | 18.2 ± 0.3 | 1.5 ± 0.3 |
| py3.9 | 51.3 ± 2.1 | 48.6–53.6 | 18.7 ± 0.3 | 1.8 ± 0.4 |
| py3.10 | 50.1 ± 4.9 | 42.4–53.8 | 18.4 ± 0.5 | 2.0 ± 0.3 |
| py3.11 | 45.6 ± 4.6 | 40.1–50.4 | 18.2 ± 0.6 | 1.4 ± 0.3 |
| py3.12 | 50.4 ± 5.9 | 43.5–56.1 | 18.4 ± 0.5 | 1.3 ± 0.3 |
| py3.13 | 55.5 ± 6.3 | 46.7–60.9 | 18.8 ± 0.5 | 1.5 ± 0.1 |
| py3.14 | 55.6 ± 5.9 | 48.6–63.1 | 18.8 ± 0.5 | 1.3 ± 0.2 |

Setup time: devcontainer 15.5 ± 2.7 s, Nix 16.4 ± 0.7 s, others 0 (interpreter already present
or installed by `setup-python` before timing starts, so not comparable).

### The devcontainer changes the cost results, not only the speed

- Run time in the container is about 2.4× the traditional setup (113.5 vs. 47.9 s;
  permutation test p ≈ 0.008, 5 vs. 5 runs).
- More importantly, the **relative** costs move in a consistent direction:
  - random-strategy cost: 22.9% in the container vs. 18.5% in all other environments
    (lowest container value 22.2%, highest other value 19.3%; p < 0.001)
  - skip_e2e cost: 0.8% vs. 1.6% (p < 0.001)
- Both point the same way: inside the container, the end-to-end tests take a larger share of
  the total time. The e2e tests start a server process and talk to it over HTTP.
- Possible cause (not yet tested): process start-up or file access through the bind-mounted
  repository is slower in the container. Next experiment: copy the code into the image instead of
  mounting it, and compare.

So a cost-based conclusion ("strategy X costs Y% of a full run") depends on the environment in
which it was measured, even when the functional results are identical.

### Other observations (single factor, not tested)

- The Nix job's run time was the most stable of all (standard deviation 0.6 s vs. 1.8–6.3 s for
  the others). With 5 runs on shared virtual machines this may be chance; it is noted, not claimed.
- "Python 3.12" was three different patch versions: 3.12.3 (traditional), 3.12.12 (Nix),
  3.12.15 (devcontainer and setup-python). All gave the same functional hash here, but only the
  Nix lock file and the image digest record which one was used.

## Threats to validity

- **Statistics treat each run as independent.** The five runs were started close together and
  may share runner hardware or load. Runs on different days would be stronger evidence.
- **The p-values compare one environment against pooled others**; they are descriptive support
  for an effect that is also visible in the raw ranges, not a formal confirmatory test.
- **The timeout mutant (M0235)** is classified by a time limit. It was stable in all 45 runs,
  but it is the one point where machine speed could leak into the functional hash.
- **Setup times are cold-start only** and exclude the time `setup-python` and the Nix installer
  take, so the setup numbers compare the devcontainer and Nix with each other only.
