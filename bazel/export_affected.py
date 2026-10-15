"""export_affected.py - ask Bazel which test targets each source file can affect.

For every file in sut/, runs
    bazel query "kind(py_test, rdeps(//..., <file>))"
(every test target that depends on the file, directly or transitively) and
writes the answer to bazel/affected_tests.json.

The mutation experiment reads that JSON as the `bazel_graph` strategy, so the
experiment itself still needs nothing but Python. Re-run this script whenever a
BUILD.bazel file changes; CI checks that the committed JSON is up to date.

Usage: python3 bazel/export_affected.py [--check]
"""
import json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "bazel" / "affected_tests.json"


def query(expr):
    out = subprocess.run(["bazel", "query", expr, "--output=label"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return sorted(line for line in out.splitlines() if line.startswith("//"))


affected = {}
for src in sorted((ROOT / "sut").glob("*.py")):
    rel = src.relative_to(ROOT).as_posix()
    label = "//sut:" + src.name
    affected[rel] = query(f"kind(py_test, rdeps(//..., {label}))")

text = json.dumps(affected, indent=2, sort_keys=True) + "\n"
if "--check" in sys.argv:
    if not OUT.exists() or OUT.read_text() != text:
        sys.exit("bazel/affected_tests.json is out of date: run python3 bazel/export_affected.py")
    print("affected_tests.json matches the BUILD files")
else:
    OUT.write_text(text)
    print(text)
