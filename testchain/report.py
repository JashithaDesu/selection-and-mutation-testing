"""Markdown report (+ optional PNG chart if matplotlib is installed)."""

import math
from pathlib import Path


def _pct(x, digits=1):
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.{digits}f}%"


def _s(x, digits=2):
    return "–" if x is None else f"{x:.{digits}f}"


def _money(x):
    return "∞" if x == float("inf") else f"{x:,.0f}"


def chart(ev, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    ink, muted, grid = "#1f2328", "#59636e", "#d8dee4"
    # Group strategies that land on (nearly) the same point so labels never collide.
    groups = []
    for name, s in ev["strategies"].items():
        x, y = max(s["cost_pct_of_all"], 0.05), 100 * s["detection_rate"]
        for g in groups:
            if abs(g["y"] - y) < 1.5 and max(g["x"], x) / min(g["x"], x) < 2.5:
                g["names"].append(name)
                g["xs"].append(x)
                break
        else:
            groups.append({"x": x, "y": y, "xs": [x], "names": [name]})
    for g in groups:
        ax.scatter(g["xs"], [g["y"]] * len(g["xs"]), s=60, color="#2f6fdb",
                   edgecolor="white", linewidth=1.5, zorder=3)
        ax.annotate("\n".join(g["names"]), (max(g["xs"]), g["y"]), textcoords="offset points",
                    xytext=(7, 0), va="center", fontsize=8, color=ink)
    ax.set_xscale("log")
    ax.set_xlim(0.2, 300)
    ax.set_xticks([0.3, 1, 3, 10, 30, 100])
    ax.set_xticklabels(["0.3%", "1%", "3%", "10%", "30%", "100%"])
    ax.set_xlabel("Mean test time per commit, % of running everything (log scale)", color=muted)
    ax.set_ylabel("Regressions detected before merge (%)", color=muted)
    ax.set_title("Test selection: cost vs. detection", color=ink, loc="left")
    ax.set_ylim(min(0, min(100 * s["detection_rate"] for s in ev["strategies"].values()) - 5), 105)
    ax.grid(True, color=grid, linewidth=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(grid)
    ax.tick_params(colors=muted)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True


def render(ev, base, mutants_by_id, out_dir, diffs):
    out_dir = Path(out_dir)
    has_chart = chart(ev, out_dir / "cost_vs_detection.png")
    pop = ev["population"]
    adq = ev["adequacy"]
    L = []
    L.append("# Test-chain experiment report\n")
    L.append(f"Generated from baseline of {base['created']} (Python {base['python']}, {base['platform']}).\n")
    L.append("## 1. Setup\n")
    stages = {}
    for t in base["tests"]:
        stages.setdefault(base["stages"][t], []).append(t)
    L.append("| Stage | Tests | Median time incl. fixtures (s) |\n|---|---:|---:|")
    for st, tests in stages.items():
        classes = {t.rsplit(".", 1)[0] for t in tests}
        secs = sum(base["durations"][t] for t in tests) + sum(base["class_overhead"][c] for c in classes)
        L.append(f"| {st} | {len(tests)} | {secs:.3f} |")
    L.append(f"\nFull chain: **{ev['run_all_seconds']:.2f}s**. Flaky tests in {base['repeat']} baseline runs: "
             f"**{len(base['flaky'])}**. Mutants generated: **{pop['mutants_total']}**, detectable by the full "
             f"suite: **{pop['detectable']}**.\n")

    L.append("## 2. Test adequacy (mutation score)\n")
    L.append("| File | Killed | Survived | Timeout | Mutation score |\n|---|---:|---:|---:|---:|")
    for f, d in sorted(adq["per_file"].items()):
        L.append(f"| `{f}` | {d['killed']} | {d['survived']} | {d['timeout']} | {_pct(d['mutation_score'])} |")
    t = adq["total"]
    L.append(f"| **total** | {t['killed']} | {t['survived']} | {t['timeout']} | **{_pct(t['mutation_score'])}** |\n")
    L.append("Timeouts count as killed (the CI job would time out). Survivors are either test gaps "
             "or equivalent mutants (changes with no observable effect); see section 6.\n")

    L.append("## 3. Where regressions are caught (shift-left profile)\n")
    sp = ev["stage_profile"]
    L.append("Earliest stage that detects each detectable mutant:\n")
    L.append("| First detecting stage | Mutants | Share |\n|---|---:|---:|")
    for st, n in sp["first_stage_counts"].items():
        L.append(f"| {st} | {n} | {_pct(n / pop['detectable'] if pop['detectable'] else 0)} |")
    L.append(f"\n**{len(sp['only_detected_by_e2e'])}** mutants are caught *only* by e2e, the most expensive stage. "
             "Each is a candidate for a new unit/integration test (shift-left opportunity).\n")

    L.append("## 4. Selection strategies\n")
    L.append("| Strategy | Detected | Rate (95% CI) | Time vs. all | Mean tests | Median feedback (ms) |\n"
             "|---|---:|---|---:|---:|---:|")
    for name, s in ev["strategies"].items():
        ci = f"{_pct(s['detection_rate'])} ({_pct(s['ci95'][0])}–{_pct(s['ci95'][1])})"
        L.append(f"| {name} | {s['detected']}/{s['n']} | {ci} | {s['cost_pct_of_all']:.1f}% | "
                 f"{s['mean_size']:.1f} | {_s(None if s['median_feedback_s'] is None else 1000 * s['median_feedback_s'], 1)} |")
    L.append("\nRandom baseline picks as many tests as coverage selection, at random; its interval is the "
             "min–max over seeds. LLM rows include the model's response latency in time and feedback.\n")
    if has_chart:
        L.append("![cost vs detection](cost_vs_detection.png)\n")
    L.append("### Paired comparison against coverage-based selection (exact McNemar, Holm-corrected)\n")
    L.append("| Strategy | Caught only by it | Caught only by coverage | p (Holm) | Mean time saved vs. all (ms, 95% CI) |\n"
             "|---|---:|---:|---:|---|")
    for c in ev["comparisons"]:
        lo, hi = c["saving_vs_all_s_ci95"]
        L.append(f"| {c['strategy']} | {c['only_this']} | {c['only_coverage']} | {c['p_holm']:.3g} | {1000 * lo:.1f} – {1000 * hi:.1f} |")
    if ev["llm"]:
        L.append("\n### LLM run diagnostics\n")
        for run, meta in ev["llm"].items():
            if meta.get("warning"):
                L.append(f"\n> **WARNING ({run}):** {meta['warning']}.\n")
            L.append(f"- **{run}**: " + ", ".join(f"{k.replace('_', ' ')} = {v if not isinstance(v, float) else round(v, 2)}"
                                                for k, v in meta.items()))
        for run, sub in ev.get("llm_subsets", {}).items():
            L.append(f"\n### Sampled LLM run `{run}`: all strategies on the same {sub['n']} mutants\n")
            L.append("| Strategy | Detected | Rate (95% CI) | Time vs. all | Mean tests |\n|---|---:|---|---:|---:|")
            for name, s in sub["strategies"].items():
                L.append(f"| {name} | {s['detected']}/{s['n']} | {_pct(s['detection_rate'])} "
                         f"({_pct(s['ci95'][0])}–{_pct(s['ci95'][1])}) | {s['cost_pct_of_all']:.1f}% | {s['mean_size']:.1f} |")
    else:
        L.append("\n_No LLM run found. Run `python -m testchain llm --backend ollama --model <model>` and re-run "
                 "`evaluate` to add LLM strategies._\n")

    L.append("\n## 5. Return on investment model\n")
    cfg = ev["roi_config"]
    L.append(f"Measured times are scaled so the full chain takes **{cfg['full_chain_minutes']} min**. "
             f"Assumptions (edit `roi.json`): {_pct(cfg['defect_commit_rate'], 0)} of commits carry a regression; "
             f"an escaped regression costs **{cfg['escape_cost_minutes']} engineer-minutes**; a CI minute is worth "
             f"{cfg['ci_minute_weight']} engineer-minutes; {_pct(cfg['dev_wait_weight'], 0)} of developer waiting "
             "time is lost. All costs below are engineer-minutes per commit.\n")
    sens = cfg["escape_cost_sensitivity"]
    L.append("| Strategy | Expected cost | Saving vs. all | Break-even escape cost | "
             + " | ".join(f"@{x}" for x in sens) + " |")
    L.append("|---|---:|---:|---:|" + "---:|" * len(sens))
    for name, r in ev["roi"].items():
        L.append(f"| {name} | {r['expected_cost_per_commit']:.2f} | {r['saving_vs_run_all']:+.2f} | "
                 f"{'–' if name == 'run_all' else _money(r['break_even_escape_cost'])} | "
                 + " | ".join(f"{r['sensitivity'][x]:.2f}" for x in sens) + " |")
    L.append("\n*Break-even escape cost*: if a regression that slips past selection costs more than this, "
             "running everything is cheaper. It is the single number to argue about with a team.\n")

    L.append("## 6. Surviving mutants (test gaps or equivalent mutants)\n")
    if adq["survivors"]:
        L.append("<details><summary>" + f"{len(adq['survivors'])} survivors" + "</summary>\n")
        for mid in adq["survivors"]:
            m = mutants_by_id[mid]
            L.append(f"**{mid}** `{m['file']}:{m['lineno']}` {m['operator']}: "
                     f"`{m['original'][:70]}` → `{m['replacement'][:70]}`  ")
        L.append("\n</details>\n")
    L.append("## 7. Test value\n")
    tv = ev["test_value"]
    zero = [t for t, v in tv.items() if v["kills"] == 0]
    top = sorted(tv.items(), key=lambda kv: -kv[1]["unique_kills"])[:8]
    L.append("Tests with the most *unique* kills (removing them loses detection):\n")
    L.append("| Test | Stage | Kills | Unique kills | ms |\n|---|---|---:|---:|---:|")
    for t, v in top:
        L.append(f"| `{t.split('.', 2)[2]}` | {v['stage']} | {v['kills']} | {v['unique_kills']} | {1000 * v['seconds']:.2f} |")
    L.append(f"\n{len(zero)} tests kill no mutant at all: " + (", ".join(f"`{t.split('.', 2)[2]}`" for t in zero[:10]) or "none")
             + (" …" if len(zero) > 10 else "") + "\n")
    L.append("## 8. Threats to validity\n")
    L.append("- **Mutants are not real regressions.** They are small, single-point faults; real commits are larger "
             "and touch several files. Mutation-based ground truth is standard in RTS research but should be "
             "complemented with real fault data (e.g. historical CI failures).\n"
             "- **Toy scale.** One small service, ~70 tests. Absolute times are meaningless; ratios and the "
             "break-even analysis are the transferable results.\n"
             "- **Equivalent mutants** inflate the survivor count and were not manually filtered.\n"
             "- **Timing noise.** Durations are medians of repeated runs on one machine; class fixture cost "
             "is attributed to the first test of the class.\n"
             "- **LLM non-determinism.** Temperature 0 and cached responses make one run reproducible, not "
             "representative; repeat with several models/seeds before generalising.\n")
    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")
    return out_dir / "report.md"
