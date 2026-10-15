# Reproducible environments: traditional vs. devcontainer vs. Nix

The mutation experiment, run in three environments and on six Python versions,
checking one question: **does the experiment give the same answer everywhere?**

| | Traditional | Devcontainer | Nix flake |
|---|---|---|---|
| Defined in | `env/TRADITIONAL.md` | `.devcontainer/` | `flake.nix` + `flake.lock` |
| What is pinned | nothing (whatever `python3` exists) | base image tag `python:3.12-slim` (digest optional) | Python and every dependency, via one nixpkgs commit |
| Needs admin rights | no | Docker access | to install Nix (creates `/nix`) |

## Why this project is a useful second subject

The other subject in this comparison, [hdl-debug-agent](https://github.com/JashithaDesu/hdl-debug-agent),
depends on a native toolchain (Icarus Verilog). This one is **standard-library Python only**:
nothing to install beyond an interpreter. That makes it the best case for the
"traditional" approach, so it tests whether Nix or a devcontainer still adds anything
when there are no dependencies to pin, except the interpreter itself.

## Functional results vs. timing results

The experiment produces two kinds of numbers, and they behave differently:

- **Functional:** which 254 mutants are generated, which tests kill each one, how many bugs
  each selection strategy catches. `env/fingerprint.py` hashes these. If the environment is
  reproducible, the hash is identical.
- **Timing:** seconds and cost percentages. These depend on the machine and the moment and
  are expected to vary. They are recorded, not hashed.

## Preliminary result (local, before CI)

Run on one machine with Python 3.11, 3.12 and 3.13, `python -m testchain all --jobs 2`:

| Python | mutants | killed / survived / timeout | functional sha256 | random strategy cost |
|---|---|---|---|---|
| 3.11 | 254 | 207 / 46 / 1 | `b6aaa330cafb` | 21.5% |
| 3.12 | 254 | 207 / 46 / 1 | `b6aaa330cafb` | 17.9% |
| 3.13 | 254 | 207 / 46 / 1 | `b6aaa330cafb` | 19.8% |

The functional results were identical across three interpreter versions. The timing-based
cost of the same strategy varied by 3.6 percentage points between runs. So for this
project, the environment did not change *what* the experiment found, but any claim
about *cost* needs repeated runs and a spread, not a single number.

The CI matrix extends this to 3.9, 3.10 and 3.14. The mutation engine generates code
with `ast.unparse`, whose output has changed between Python versions, so a different
hash on older or newer versions is plausible and would be a real finding: the same
repository, with no dependencies at all, giving a different experiment depending on
which `python3` happens to be installed.

## What is measured (`.github/workflows/environments.yml`)

Each job runs on a fresh runner (a cold start):

1. **Setup time** from nothing to a working interpreter (0 for traditional: it uses what the runner has).
2. **Run time** and exit code of `python -m testchain all --jobs 4`.
3. **Functional fingerprint** and mutant status counts.
4. **Rebuild reproducibility:**
   - Devcontainer: build the image twice with `--no-cache` and compare image IDs.
   - Nix: `nix build .#experiment` runs the whole experiment in the sandbox (no network,
     no home folder) and outputs only the functional fingerprint. `--rebuild` runs it again
     and fails if the output differs by a single byte.
5. **Bill of materials:** an SPDX SBOM of the image (Syft), the base image digest, the
   full Nix closure and the exact nixpkgs revision.

The `compare` job prints one table for all environments and a second table of the timing
spread. Run the workflow from **Actions → environments → Run workflow** at least five times
and record each summary in `env/results.md`.

## First steps

1. Copy these files into the repository, commit and push. Trigger the workflow manually.
2. Download `flake.lock` from the `env-nix` artifact, commit it and push. Until then,
   nixpkgs is not truly pinned.
3. `ci.yml` is unchanged. It is still the staged test chain; this workflow is the
   environment experiment and only runs when environment files change or on demand.

## Threats to validity (know these before an interview)

- **The one timeout mutant (M0235)** is classified by a time limit, not by a test result.
  On a much slower machine, a mutant close to the limit could flip between "killed" and
  "timeout". It stayed stable in all local runs, but it is the one place where timing can
  leak into the functional hash.
- **GitHub runners are shared virtual machines.** Timing numbers from CI say little about
  absolute speed; compare spreads, not single values.
- **Setup times are cold-start only.** Nix with a binary cache and Docker with layer
  caching would both be much faster on a second run. That is a separate experiment.

## Threat model

Use the same questions as in hdl-debug-agent (`env/THREAT_MODEL.md` there), and answer
one extra question for this project: when a project has no dependencies, what is left in
its supply chain? (The interpreter, the base image, the CI actions and the runner image.)
