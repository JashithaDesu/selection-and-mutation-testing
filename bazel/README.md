# Bazel

The test suite builds and runs with Bazel 8 (pinned in `.bazelversion`), using `rules_python`
with a hermetic Python 3.12 toolchain: Bazel downloads its own interpreter instead of using
whatever `python3` the machine has.

```bash
bazel test //...                     # all 7 test targets (72 tests)
bazel test //tests/unit/...          # one stage
bazel run //sut:gateway -- --port 8080
bazel query "kind(py_test, rdeps(//..., //sut:router.py))"   # which tests can a change to router.py affect?
```

## The graph

| target | declared dependencies |
|---|---|
| `//sut:handlers`, `:autoscaler`, `:ratelimit` | none (only the package) |
| `//sut:registry` | handlers |
| `//sut:router` | autoscaler, handlers, ratelimit, registry |
| `//sut:server` | ratelimit, registry, router |
| `//tests:support` | `//sut:registry` |
| `//tests/unit:test_*` | the module under test (+ support) |
| `//tests/integration:test_gateway_flow` | everything the integration test imports |
| `//tests/e2e:test_api` | `//sut:server`, declared by hand (the test never imports it) |

Every dependency comes from a real `import`. `check_graph.py` reads the imports of every source
file with `ast` and fails if a target imports a project module it does not depend on.

## What it adds to the experiment

`export_affected.py` asks Bazel, for each file in `sut/`, which test targets depend on it, and
writes `affected_tests.json`. The mutation experiment reads that file as the **`bazel_graph`**
strategy: for each of the 207 detectable bugs, rerun exactly the test targets `bazel test //...`
would rerun after that change, with everything else coming from the cache.

| strategy | bugs caught | test time vs. running everything |
|---|---|---|
| line coverage | 87.0% | 0.8% |
| coverage + always e2e | 97.1% | 98.1% |
| **bazel_graph** | **100% (207/207)** | **99.2%** |

- It is the only strategy besides running everything that catches every bug. Line coverage misses
  import-time code; a file-level graph does not.
- It saves almost nothing. Every `sut` module is reachable from the server, the e2e target depends
  on the server, so every change reruns e2e, which is 98% of the suite's time.
- Bazel's caching only pays off when expensive tests depend on a *small* part of the code.

The CI job checks this against real Bazel: it touches each source file in turn and counts the
targets Bazel reruns. Locally: `__init__` 7, `autoscaler` 4, `handlers` 7, `ratelimit` 4,
`registry` 6, `router` 3, `server` 2 of 7, matching `affected_tests.json` exactly.

## A dependency bug that Bazel did not catch

The e2e test located the project with `Path(__file__).resolve()`. Under Bazel the test file is a
symlink in the runfiles tree, and `resolve()` follows it back into the source tree. So the test
ran the server from the source tree, not from the files Bazel gave it.

Consequence, reproduced:

1. Remove `//sut:server` from the e2e target's deps. The test still passes.
2. Break the server's startup message. `bazel test //...` reports
   `//tests/e2e:test_api (cached) PASSED`: Bazel sees no declared input change and reuses the old
   result, while plain `python -m unittest` fails.

Fix: `Path(__file__).absolute()`, which does not follow the symlink. Now the test can only see its
declared runfiles, and a missing dependency fails loudly (`server failed to start`) instead of
passing silently.

Lesson: Bazel's correctness guarantee covers *declared* inputs. A test that reaches outside its
runfiles breaks it without any warning, and the sandbox did not prevent it here.

## Keeping it correct

After changing any `BUILD.bazel` file:

```bash
python3 bazel/check_graph.py        # every import is a declared dependency
python3 bazel/export_affected.py    # regenerate affected_tests.json, then commit it
```

CI fails if either is out of date. `MODULE.bazel.lock` pins the exact module versions: download it
from the `env-bazel` artifact of the first CI run and commit it, as with `flake.lock`.
