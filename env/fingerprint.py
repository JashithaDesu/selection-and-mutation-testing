"""fingerprint.py - a hash of the experiment's *functional* results.

The experiment writes two kinds of numbers:
  - functional: which mutants exist, which tests kill each one, how many
    bugs each selection strategy catches. These should be identical on
    every machine and every run if the environment is reproducible.
  - timing: seconds, cost percentages, feedback times. These depend on
    the machine and the moment, so they are expected to vary.

This script hashes only the functional part, so runs in different
environments can be compared with one line. It also prints the timing
numbers separately, so their variation can be measured too.

Usage: python3 env/fingerprint.py [results_dir] [--functional-only]  ->  prints JSON

--functional-only drops the timing block. The Nix build uses it, because a
sandboxed build must produce byte-identical output to pass `--rebuild`.
"""
import hashlib, json, os, sys

args = [a for a in sys.argv[1:] if not a.startswith("--")]
functional_only = "--functional-only" in sys.argv
res = args[0] if args else "results"
load = lambda name: json.load(open(os.path.join(res, name)))

mutants = load("mutants.json")
kill = load("kill_matrix.json")
ev = load("evaluation.json")

functional = {
    "mutants": mutants,
    "kill_outcomes": {m: [d["status"], sorted(d["killed_by"])] for m, d in kill.items()},
    "strategies": {name: {k: s[k] for k in ("n", "detected", "escaped", "mean_size") if k in s}
                   for name, s in ev["strategies"].items()},
}
digest = lambda obj: hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()

statuses = {}
for status, _ in functional["kill_outcomes"].values():
    statuses[status] = statuses.get(status, 0) + 1

out = {
    "functional_sha256": digest(functional),
    "mutants_sha256": digest(mutants),
    "kill_outcomes_sha256": digest(functional["kill_outcomes"]),
    "n_mutants": len(mutants),
    "statuses": dict(sorted(statuses.items())),
}
if not functional_only:
    out["timing"] = {
        "run_all_seconds": ev.get("run_all_seconds"),
        "cost_pct": {name: round(s.get("cost_pct_of_all", 0), 1) for name, s in ev["strategies"].items()},
    }
print(json.dumps(out, indent=2, sort_keys=True))
