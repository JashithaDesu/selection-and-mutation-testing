"""check_graph.py - does the Bazel graph match the code's real imports?

Bazel only reruns a test when something it *declares* changed. A missing
dependency in a BUILD file means a changed module is silently skipped and a
cached "PASSED" is reported. This script compares, for every target, the
project modules its sources import (read with `ast`) with the targets it
depends on in BUILD.bazel (directly or transitively), and fails on any import that is not declared.

It cannot see dependencies that are not imports, such as the e2e test talking
to the server over HTTP. Those must be declared by hand (see tests/e2e/BUILD.bazel)
and are protected by the test running from Bazel's runfiles only.

Usage: python3 bazel/check_graph.py
"""
import ast, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bazel(*args):
    return subprocess.run(["bazel", *args], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout


def project_imports(py_file):
    """Modules under sut/ or tests/ that a file imports, as 'sut/router.py' paths."""
    tree = ast.parse((ROOT / py_file).read_text())
    found = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for n in names:
            if n.split(".")[0] in ("sut", "tests"):
                path = n.replace(".", "/") + ".py"
                if (ROOT / path).exists():
                    found.add(path)
    return found


targets = [t for t in bazel("query", "kind('py_(library|test|binary)', //...)").split() if t.startswith("//")]
problems = 0
for target in targets:
    pkg = target[2:].split(":")[0]
    srcs = [l.split(":")[1] for l in bazel("query", f"labels(srcs, {target})").split() if l.startswith("//")]
    declared = set()
    for dep in bazel("query", f"kind(rule, deps({target})) except {target}").split():
        if dep.startswith("//"):
            dpkg, _ = dep[2:].split(":")
            for s in bazel("query", f"labels(srcs, {dep})").split():
                if s.startswith("//"):
                    declared.add(f"{dpkg}/{s.split(':')[1]}")
    for src in srcs:
        own = f"{pkg}/{src}"
        for imp in sorted(project_imports(own) - declared - {own}):
            print(f"MISSING  {target}: {own} imports {imp}, which is not a declared dependency")
            problems += 1

print(f"checked {len(targets)} targets: {'OK' if not problems else f'{problems} undeclared imports'}")
sys.exit(1 if problems else 0)
