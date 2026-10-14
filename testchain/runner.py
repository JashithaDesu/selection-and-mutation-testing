"""Runs the test suite against the original project and against every mutant.

Each parallel slot gets its own scratch copy of the project, so mutants never
touch the real source tree and parallel runs never interfere. Works the same on
Windows and Linux (threads launch subprocesses; no fork()).
"""

import ast
import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORKER = Path(__file__).with_name("worker.py")
STAGES = ("unit", "integration", "e2e")


def stage_of(test_id):
    parts = test_id.split(".")
    return parts[1] if len(parts) > 1 and parts[1] in STAGES else "unknown"


def class_of(test_id):
    return test_id.rsplit(".", 1)[0]


def module_of(test_id):
    return test_id.rsplit(".", 2)[0]


def make_copy(project_root, source_dir, tests_dir, dest):
    for d in (source_dir, tests_dir):
        shutil.copytree(Path(project_root) / d, Path(dest) / d,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def run_worker(project_dir, coverage=None, tests_dir="tests", timeout=None):
    """Run the worker in `project_dir`. Returns parsed JSON, or None on timeout."""
    fd, out = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    cmd = [sys.executable, "-B", str(WORKER), "--out", out, "--tests", tests_dir]
    if coverage:
        cmd += ["--coverage", coverage]
    env = dict(os.environ, PYTHONPATH=str(project_dir), PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONUNBUFFERED", None)  # same I/O behaviour on every machine
    try:
        proc = subprocess.run(cmd, cwd=project_dir, env=env, timeout=timeout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            return {"crashed": True, "stderr": proc.stderr[-2000:]}
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)
    except subprocess.TimeoutExpired:
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def static_imports(project_root, tests_dir, source_dir):
    """{test_module: [sut modules imported directly]} via AST (no execution)."""
    result = {}
    root = Path(project_root)
    for path in sorted((root / tests_dir).rglob("test_*.py")):
        mod = ".".join(path.relative_to(root).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == source_dir:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == source_dir:
                        names.add(alias.name)
        result[mod] = sorted(names)
    return result


def baseline(project_root, source_dir="sut", tests_dir="tests", repeat=3, log=print):
    """Run the unmutated suite `repeat` times (plus one traced run for coverage).

    Repeating serves two purposes: median timings are less noisy, and any test
    whose outcome differs between runs is flagged as flaky. A flaky test would
    corrupt the kill matrix (a mutant 'killed' by noise), so the harness
    refuses to continue if one is found.
    """
    with tempfile.TemporaryDirectory(prefix="tcl-base-") as tmp:
        make_copy(project_root, source_dir, tests_dir, tmp)
        runs = []
        for i in range(repeat):
            r = run_worker(tmp, tests_dir=tests_dir, timeout=600)
            if r is None or r.get("crashed"):
                raise RuntimeError(f"baseline run {i + 1} failed: {r and r.get('stderr')}")
            runs.append(r)
            log(f"  baseline run {i + 1}/{repeat}: {len(r['tests'])} tests in {r['wall_time']:.2f}s")
        traced = run_worker(tmp, coverage=source_dir, tests_dir=tests_dir, timeout=600)
        if traced is None or traced.get("crashed"):
            raise RuntimeError("coverage run failed")

    tests = runs[0]["tests"]
    failing = sorted({t for r in runs for t, o in r["outcomes"].items() if o in ("fail", "error")})
    flaky = sorted({t for t in tests if len({r["outcomes"].get(t) for r in runs}) > 1})
    if runs[0]["load_errors"]:
        raise RuntimeError(f"test modules failed to import: {runs[0]['load_errors']}")
    durations = {t: statistics.median(r["durations"][t] for r in runs) for t in tests}
    classes = sorted({class_of(t) for t in tests})
    overhead = {c: statistics.median(r["class_overhead"].get(c, 0.0) for r in runs) for c in classes}
    stage_order = {s: i for i, s in enumerate(STAGES)}
    tests_sorted = sorted(tests, key=lambda t: (stage_order.get(stage_of(t), 99), tests.index(t)))
    return {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "repeat": repeat,
        "tests": tests_sorted,
        "stages": {t: stage_of(t) for t in tests},
        "durations": durations,
        "class_overhead": overhead,
        "wall_times": [r["wall_time"] for r in runs],
        "coverage": traced["coverage"],
        "imports": static_imports(project_root, tests_dir, source_dir),
        "failing": failing,
        "flaky": flaky,
    }


def _killed_by(result, tests):
    """Tests that detect the mutant: failed, errored, or never ran because their
    module could not be imported."""
    failed = {t for t, o in result["outcomes"].items() if o in ("fail", "error")}
    for mod in result.get("load_errors", []):
        failed.update(t for t in tests if t == mod or t.startswith(mod + "."))
    return sorted(failed)


def kill_matrix(project_root, mutants, base, source_dir="sut", tests_dir="tests",
                jobs=None, log=print):
    jobs = jobs or max(1, min(8, (os.cpu_count() or 2)))
    timeout = max(10.0, 5 * max(base["wall_times"]) + 5)
    tests = base["tests"]
    results = {}
    slots = queue.Queue()
    tmp_root = tempfile.mkdtemp(prefix="tcl-mut-")
    try:
        for i in range(jobs):
            d = Path(tmp_root) / f"slot{i}"
            make_copy(project_root, source_dir, tests_dir, d)
            slots.put(d)
        originals = {}

        def run_one(m):
            slot = slots.get()
            target = slot / m.file
            try:
                original = originals.setdefault(m.file, (Path(project_root) / m.file).read_bytes())
                target.write_bytes(m.apply(original))
                t0 = time.perf_counter()
                r = run_worker(slot, tests_dir=tests_dir, timeout=timeout)
                elapsed = time.perf_counter() - t0
                if r is None:
                    entry = {"status": "timeout", "killed_by": [], "seconds": elapsed}
                elif r.get("crashed"):
                    entry = {"status": "crash", "killed_by": [], "seconds": elapsed,
                             "detail": r.get("stderr", "")[-300:]}
                else:
                    killed = _killed_by(r, tests)
                    entry = {"status": "killed" if killed else "survived",
                             "killed_by": killed, "seconds": elapsed}
                return m.id, entry
            finally:
                target.write_bytes(originals[m.file])
                slots.put(slot)

        done = 0
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for mid, entry in pool.map(run_one, mutants):
                results[mid] = entry
                done += 1
                if done % 25 == 0 or done == len(mutants):
                    log(f"  {done}/{len(mutants)} mutants ({time.perf_counter() - start:.0f}s)")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return results
