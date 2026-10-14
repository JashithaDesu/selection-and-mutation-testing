"""testchain - measure a staged test chain and compare test-selection strategies.

    python -m testchain all                   # baseline -> mutants -> kill matrix -> evaluate
    python -m testchain baseline --repeat 3
    python -m testchain mutants
    python -m testchain killmatrix --jobs 4
    python -m testchain llm --backend ollama --model qwen2.5-coder:7b
    python -m testchain evaluate
    python -m testchain show M0042             # diff + which tests kill it
"""

import argparse
import json
import sys
import time
from pathlib import Path

from testchain import evaluate as ev_mod
from testchain import llm as llm_mod
from testchain import report, runner
from testchain.mutate import Mutant, generate


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=lambda o: None if o == float("inf") else str(o))


class Paths:
    def __init__(self, root, results):
        self.root = Path(root).resolve()
        self.results = (self.root / results).resolve()
        self.baseline = self.results / "baseline.json"
        self.mutants = self.results / "mutants.json"
        self.km = self.results / "kill_matrix.json"
        self.evaluation = self.results / "evaluation.json"
        self.llm_cache = self.results / "llm_cache"


def cmd_baseline(p, args):
    print(f"Running baseline ({args.repeat} runs + 1 coverage run)...")
    base = runner.baseline(p.root, args.source, args.tests, repeat=args.repeat)
    if base["failing"]:
        sys.exit(f"Baseline has failing tests, fix them first: {base['failing']}")
    if base["flaky"]:
        sys.exit(f"Flaky tests detected, the kill matrix would be unreliable: {base['flaky']}")
    _save(p.baseline, base)
    print(f"  {len(base['tests'])} tests, 0 flaky -> {p.baseline}")


def cmd_mutants(p, args):
    ms = generate(p.root, args.source)
    _save(p.mutants, [m.to_dict() for m in ms])
    print(f"  {len(ms)} mutants -> {p.mutants}")


def cmd_killmatrix(p, args):
    base, ms = _load(p.baseline), [Mutant.from_dict(d) for d in _load(p.mutants)]
    if args.limit:
        ms = ms[:args.limit]
    print(f"Running full suite against {len(ms)} mutants...")
    t0 = time.perf_counter()
    km = runner.kill_matrix(p.root, ms, base, args.source, args.tests, jobs=args.jobs)
    _save(p.km, km)
    counts = {}
    for e in km.values():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    print(f"  {counts} in {time.perf_counter() - t0:.0f}s -> {p.km}")


def _diffs(p, ms):
    sources = {}
    out = {}
    for m in ms:
        src = sources.setdefault(m.file, (p.root / m.file).read_bytes())
        out[m.id] = m.diff(src)
    return out


def cmd_llm(p, args):
    base, km = _load(p.baseline), _load(p.km)
    ms = [Mutant.from_dict(d) for d in _load(p.mutants)]
    ms = [m for m in ms if km.get(m.id, {}).get("status") == "killed"]
    if args.sample and args.sample < len(ms):
        import random
        ms = sorted(random.Random(args.seed).sample(ms, args.sample), key=lambda m: m.id)
    catalogue = llm_mod.test_catalogue(p.root, base["tests"])
    diffs = _diffs(p, ms)
    if args.print_prompt:
        print(llm_mod.SYSTEM_PROMPT + "\n\n" + llm_mod.build_prompt(diffs[ms[0].id], catalogue))
        return
    backend = llm_mod.BACKENDS[args.backend](args.model, timeout=args.timeout)
    print(f"Asking {args.backend}:{args.model} to select tests for {len(ms)} mutants...")
    records = llm_mod.select_for_mutants(backend, ms, diffs, catalogue, p.llm_cache)
    out = p.results / f"llm_{llm_mod.slug(args.backend, args.model)}.json"
    _save(out, records)
    print(f"  -> {out}")


def cmd_evaluate(p, args):
    base, km, mut = _load(p.baseline), _load(p.km), _load(p.mutants)
    mut = [m for m in mut if m["id"] in km]
    llm_runs = {f.stem[4:]: _load(f) for f in sorted(p.results.glob("llm_*.json"))}
    roi_path = p.root / "roi.json"
    roi_cfg = _load(roi_path) if roi_path.exists() else None
    ev = ev_mod.evaluate(base, mut, km, llm_runs=llm_runs, roi_cfg=roi_cfg)
    _save(p.evaluation, ev)
    by_id = {m["id"]: m for m in mut}
    path = report.render(ev, base, by_id, p.results, None)
    print(f"  report -> {path}")
    w = max(len(n) for n in ev["strategies"])
    print(f"\n  {'strategy':<{w}}  detect   time   break-even")
    for name, s in ev["strategies"].items():
        be = ev["roi"][name]["break_even_escape_cost"]
        print(f"  {name:<{w}}  {100 * s['detection_rate']:5.1f}%  {s['cost_pct_of_all']:5.1f}%  "
              f"{'-' if name == 'run_all' else ('inf' if be == float('inf') else f'{be:,.0f}')}")


def cmd_show(p, args):
    ms = {d["id"]: Mutant.from_dict(d) for d in _load(p.mutants)}
    m = ms[args.mutant]
    print(m.diff((p.root / m.file).read_bytes()))
    if p.km.exists():
        e = _load(p.km).get(m.id)
        if e:
            print(f"status: {e['status']}")
            for t in e["killed_by"]:
                print(f"  killed by {t}")


def main(argv=None):
    # Progress must reach log files immediately, even under nohup/redirects.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(prog="testchain", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=".", help="project root (contains sut/ and tests/)")
    ap.add_argument("--results", default="results")
    ap.add_argument("--source", default="sut")
    ap.add_argument("--tests", default="tests")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline"); b.add_argument("--repeat", type=int, default=3)
    sub.add_parser("mutants")
    k = sub.add_parser("killmatrix"); k.add_argument("--jobs", type=int); k.add_argument("--limit", type=int)
    llm = sub.add_parser("llm")
    llm.add_argument("--backend", choices=sorted(llm_mod.BACKENDS), default="ollama")
    llm.add_argument("--model", default="qwen2.5-coder:7b")
    llm.add_argument("--sample", type=int, help="random sample of N detectable mutants (faster)")
    llm.add_argument("--seed", type=int, default=1)
    llm.add_argument("--timeout", type=int, default=900, help="seconds per request")
    llm.add_argument("--print-prompt", action="store_true", help="print one prompt and exit")
    sub.add_parser("evaluate")
    s = sub.add_parser("show"); s.add_argument("mutant")
    a = sub.add_parser("all"); a.add_argument("--repeat", type=int, default=3); a.add_argument("--jobs", type=int)
    args = ap.parse_args(argv)
    p = Paths(args.project, args.results)

    if args.cmd == "all":
        args.limit = None
        for step in (cmd_baseline, cmd_mutants, cmd_killmatrix, cmd_evaluate):
            step(p, args)
        return 0
    {"baseline": cmd_baseline, "mutants": cmd_mutants, "killmatrix": cmd_killmatrix,
     "llm": cmd_llm, "evaluate": cmd_evaluate, "show": cmd_show}[args.cmd](p, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
