"""Turns the kill matrix into answers.

Population: mutants killed by the full suite ("detectable"). A mutant no test
can kill tells you about test *adequacy* (reported separately), not about
*selection*, so it is excluded from the strategy comparison.

Cost model for running an ordered selection:
  time = sum(test durations) + fixture overhead of every test class touched
(class fixtures such as starting the e2e server are paid once per class, and
only if at least one of its tests is selected).

Feedback time = elapsed time until the first failing test finishes, with the
selection run in chain order. This is the "fail fast" number developers feel.
"""

import zlib
from statistics import mean, median

from testchain import stats
from testchain.runner import STAGES, class_of
from testchain.strategies import STATIC_STRATEGIES, Context, random_matched

DEFAULT_ROI = {
    "full_chain_minutes": 60,       # how long the full chain takes at your scale
    "defect_commit_rate": 0.10,     # share of commits that introduce a regression
    "escape_cost_minutes": 480,     # engineer-minutes to handle a regression found after merge
    "ci_minute_weight": 0.05,       # 1 CI machine-minute in engineer-minute units
    "dev_wait_weight": 0.25,        # share of waiting time that is actually lost
    "escape_cost_sensitivity": [60, 240, 480, 960, 1920, 3840],
}


def simulate(ctx, ordered, killers):
    base = ctx.base
    t, seen, first = 0.0, set(), None
    for test in ordered:
        c = class_of(test)
        if c not in seen:
            t += base["class_overhead"].get(c, 0.0)
            seen.add(c)
        t += base["durations"][test]
        if first is None and test in killers:
            first = t
    return t, first


def evaluate_selections(ctx, population, select):
    """select(mutant) -> ordered list. Returns per-mutant rows."""
    rows = []
    for m, killers in population:
        chosen = select(m)
        cost, first = simulate(ctx, chosen, killers)
        rows.append({"mutant": m["id"], "detected": first is not None, "cost": cost,
                     "feedback": first, "size": len(chosen)})
    return rows


def summarise(rows, all_cost, extra_latency_s=0.0):
    n = len(rows)
    k = sum(r["detected"] for r in rows)
    fb = [r["feedback"] for r in rows if r["detected"]]
    lo, hi = stats.wilson(k, n)
    mean_cost = mean(r["cost"] for r in rows) + extra_latency_s
    return {
        "n": n, "detected": k, "escaped": n - k,
        "detection_rate": k / n if n else float("nan"), "ci95": [lo, hi],
        "mean_cost_s": mean_cost,
        "cost_pct_of_all": 100 * mean_cost / all_cost if all_cost else float("nan"),
        "mean_size": mean(r["size"] for r in rows),
        "median_feedback_s": (median(fb) + extra_latency_s) if fb else None,
        "mean_buggy_wait_s": mean((r["feedback"] if r["detected"] else r["cost"]) for r in rows) + extra_latency_s,
        "extra_latency_s": extra_latency_s,
    }


def roi(summary, all_summary, all_cost, cfg):
    scale = cfg["full_chain_minutes"] / (all_cost or 1.0)  # minutes per measured second
    p = cfg["defect_commit_rate"]

    def fixed(s):
        cost_min = s["mean_cost_s"] * scale
        wait_buggy = s["mean_buggy_wait_s"] * scale
        return cfg["ci_minute_weight"] * cost_min + cfg["dev_wait_weight"] * (p * wait_buggy + (1 - p) * cost_min)

    miss = 1 - summary["detection_rate"]
    a_s, a_all = fixed(summary), fixed(all_summary)
    per_commit = {x: a_s + p * miss * x for x in cfg["escape_cost_sensitivity"]}
    if miss == 0:
        break_even = float("inf") if a_s < a_all else 0.0
    else:
        break_even = max(0.0, (a_all - a_s) / (p * miss))
    return {
        "expected_cost_per_commit": a_s + p * miss * cfg["escape_cost_minutes"],
        "run_all_cost_per_commit": a_all,
        "saving_vs_run_all": a_all - (a_s + p * miss * cfg["escape_cost_minutes"]),
        "break_even_escape_cost": break_even,
        "sensitivity": per_commit,
    }


def stage_profile(population, stages):
    order = {s: i for i, s in enumerate(STAGES)}
    first_stage = {}
    for m, killers in population:
        first_stage[m["id"]] = min((stages[t] for t in killers), key=lambda s: order.get(s, 99))
    counts = {s: sum(1 for v in first_stage.values() if v == s) for s in STAGES}
    only_e2e = [mid for mid, s in first_stage.items() if s == "e2e"]
    return {"first_stage_counts": counts, "only_detected_by_e2e": only_e2e}


def adequacy(mutants, km):
    per_file = {}
    for m in mutants:
        st = km[m["id"]]["status"]
        f = per_file.setdefault(m["file"], {"killed": 0, "survived": 0, "timeout": 0, "crash": 0})
        f[st] += 1
    total = {k: sum(v[k] for v in per_file.values()) for k in ("killed", "survived", "timeout", "crash")}

    def score(d):
        # timeouts are detected in practice (the CI job times out), so they count as killed
        denom = d["killed"] + d["survived"] + d["timeout"]
        return (d["killed"] + d["timeout"]) / denom if denom else float("nan")

    for d in list(per_file.values()) + [total]:
        d["mutation_score"] = score(d)
    survivors = [m["id"] for m in mutants if km[m["id"]]["status"] == "survived"]
    return {"per_file": per_file, "total": total, "survivors": survivors}


def test_value(ctx, population):
    """Per test: how many mutants it kills, how many only it kills, cost."""
    kills = {t: 0 for t in ctx.tests}
    unique = {t: 0 for t in ctx.tests}
    for _, killers in population:
        for t in killers:
            kills[t] += 1
        if len(killers) == 1:
            unique[next(iter(killers))] += 1
    return {t: {"kills": kills[t], "unique_kills": unique[t], "stage": ctx.stage[t],
                "seconds": ctx.base["durations"][t]} for t in ctx.tests}


def _mode_share(records):
    if not records:
        return 0.0
    counts = {}
    for r in records:
        key = tuple(sorted(r["selected"]))
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) / len(records)


def evaluate(base, mutants, km, llm_runs=None, roi_cfg=None, random_seeds=30):
    cfg = dict(DEFAULT_ROI, **(roi_cfg or {}))
    ctx = Context(base)
    population = [(m, set(km[m["id"]]["killed_by"])) for m in mutants if km[m["id"]]["status"] == "killed"]
    all_cost, _ = simulate(ctx, ctx.tests, set())

    per_mutant, summaries = {}, {}
    for name, fn in STATIC_STRATEGIES.items():
        rows = evaluate_selections(ctx, population, lambda m, fn=fn: fn(ctx, m))
        per_mutant[name] = rows
        summaries[name] = summarise(rows, all_cost)

    # Random baseline, same size as coverage selection, averaged over seeds.
    cov_size = {r["mutant"]: max(1, r["size"]) for r in per_mutant["coverage"]}
    seed_summaries = []
    for seed in range(random_seeds):
        rows = evaluate_selections(ctx, population,
                                   lambda m, s=seed: random_matched(ctx, m, cov_size[m["id"]], s * 7919 + zlib.crc32(m["id"].encode())))
        seed_summaries.append(summarise(rows, all_cost))
    rates = [s["detection_rate"] for s in seed_summaries]
    rnd = dict(seed_summaries[0])
    rnd.update({"detection_rate": mean(rates), "ci95": [min(rates), max(rates)],
                "mean_cost_s": mean(s["mean_cost_s"] for s in seed_summaries),
                "cost_pct_of_all": mean(s["cost_pct_of_all"] for s in seed_summaries),
                "detected": round(mean(s["detected"] for s in seed_summaries), 1),
                "note": f"mean over {random_seeds} seeds; ci95 shows min-max across seeds"})
    rnd["escaped"] = rnd["n"] - rnd["detected"]
    summaries["random(size=coverage)"] = rnd

    llm_meta, llm_subsets = {}, {}
    for run_name, records in (llm_runs or {}).items():
        sub = [(m, k) for m, k in population if m["id"] in records]
        if not sub:
            llm_meta[run_name] = {"skipped": "no overlap with detectable mutants"}
            continue
        latency = mean(records[m["id"]]["latency_s"] for m, _ in sub)
        llm_strats = {
            f"llm:{run_name}": lambda m: records[m["id"]]["selected"],
            f"llm+file_coverage:{run_name}":
                lambda m: list(records[m["id"]]["selected"]) + STATIC_STRATEGIES["file_coverage"](ctx, m),
        }
        if len(sub) == len(population):
            for label, pick in llm_strats.items():
                rows = evaluate_selections(ctx, population, lambda m, pick=pick: ctx.ordered(pick(m)))
                per_mutant[label] = rows
                summaries[label] = summarise(rows, all_cost, extra_latency_s=latency)
        else:
            # Sampled run: compare everything on the same subset only.
            table = {}
            for name, fn in STATIC_STRATEGIES.items():
                table[name] = summarise(evaluate_selections(ctx, sub, lambda m, fn=fn: fn(ctx, m)), all_cost)
            for label, pick in llm_strats.items():
                rows = evaluate_selections(ctx, sub, lambda m, pick=pick: ctx.ordered(pick(m)))
                table[label] = summarise(rows, all_cost, extra_latency_s=latency)
            llm_subsets[run_name] = {"n": len(sub), "strategies": table}
        recs = [records[m["id"]] for m, _ in sub]
        llm_meta[run_name] = {
            "mutants_covered": f"{len(sub)}/{len(population)}",
            "mean_latency_s": latency,
            "parse_failures": sum(not r["parse_ok"] for r in recs),
            "errors": sum(bool(r.get("error")) for r in recs),
            "responses_with_hallucinated_ids": sum(bool(r["hallucinated"]) for r in recs),
            "hallucinated_ids_total": sum(len(r["hallucinated"]) for r in recs),
            "empty_selections": sum(not r["selected"] for r in recs),
            # Validity check: if most answers are the same list, the model is
            # not reading the diffs (e.g. copying a prompt example).
            "most_common_selection_share": round(_mode_share(recs), 2),
        }
        if _mode_share(recs) >= 0.5 and len(recs) >= 5:
            llm_meta[run_name]["warning"] = ("over half of the answers are the identical test list; "
                                             "the model is likely ignoring the diff - treat this run as invalid")

    # Paired significance: each selective strategy vs coverage-based RTS.
    comparisons = []
    ref = {r["mutant"]: r["detected"] for r in per_mutant["coverage"]}
    for name, rows in per_mutant.items():
        if name in ("coverage", "run_all"):
            continue
        b = sum(1 for r in rows if r["detected"] and not ref[r["mutant"]])
        c = sum(1 for r in rows if not r["detected"] and ref[r["mutant"]])
        savings = [ra["cost"] - r["cost"] for ra, r in zip(per_mutant["run_all"], rows)]
        comparisons.append({"strategy": name, "vs": "coverage", "only_this": b, "only_coverage": c,
                            "p": stats.mcnemar_exact(b, c),
                            "saving_vs_all_s_ci95": stats.bootstrap_mean_ci(savings)})
    for c, adj in zip(comparisons, stats.holm([c["p"] for c in comparisons])):
        c["p_holm"] = adj

    roi_table = {name: roi(s, summaries["run_all"], all_cost, cfg) for name, s in summaries.items()}
    return {
        "population": {"mutants_total": len(mutants), "detectable": len(population)},
        "run_all_seconds": all_cost,
        "strategies": summaries,
        "comparisons": comparisons,
        "roi": roi_table,
        "roi_config": cfg,
        "stage_profile": stage_profile(population, base["stages"]),
        "adequacy": adequacy(mutants, km),
        "test_value": test_value(ctx, population),
        "llm": llm_meta,
        "llm_subsets": llm_subsets,
        "per_mutant": per_mutant,
    }
