"""Summarize the element-selection experiment.

Reads results/main.jsonl (offline conditions), results/agent.jsonl (agent
loop) and data/decisions.jsonl, and writes:

  results/summary.json   every number the paper quotes
  results/misses.csv     every wrong answer, for error analysis
  results/fig_*.png      charts

Confidence intervals are 95% bootstrap intervals that resample decision
points (not individual calls), since repeats of one point are not
independent.
"""

import csv
import json
import os
import random
import statistics
from collections import defaultdict

RESULTS = "results"
BOOT = 2000


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def boot_ci(by_point, seed=11):
    """by_point: {point_id: [0/1, ...]} -> (mean, lo, hi) over points."""
    pts = list(by_point.values())
    if not pts:
        return None, None, None
    point_means = [sum(v) / len(v) for v in pts]
    mean = sum(point_means) / len(point_means)
    rng = random.Random(seed)
    stats = []
    for _ in range(BOOT):
        sample = [point_means[rng.randrange(len(point_means))] for _ in point_means]
        stats.append(sum(sample) / len(sample))
    stats.sort()
    return mean, stats[int(0.025 * BOOT)], stats[int(0.975 * BOOT) - 1]


def group_of(case_type):
    if case_type.startswith("repeated"):
        return "repeated"
    if case_type.startswith("lookalike"):
        return "lookalike-dropdown"
    return case_type


def main():
    rows = [r for r in load_jsonl(f"{RESULTS}/main.jsonl") if r.get("error") is None]
    errors = [r for r in load_jsonl(f"{RESULTS}/main.jsonl") if r.get("error") is not None]
    agent = load_jsonl(f"{RESULTS}/agent.jsonl")
    points = {p["id"]: p for p in load_jsonl("data/decisions.jsonl")}
    # Recompute correctness from the current labels so a label fix in the
    # dataset applies to results that were already collected.
    for r in rows:
        p = points[r["point_id"]]
        r["correct"] = (r["answer"] in p.get("labels", [p["label"]])) if p["kind"] == "pick" \
            else (r["answer"] == p["label"])

    summary = {"n_points": {
        "pick": sum(1 for p in points.values() if p["kind"] == "pick"),
        "verify": sum(1 for p in points.values() if p["kind"] == "verify"),
        "enumeration_misses": sum(1 for p in points.values() if p["kind"] == "pick" and p["label"] is None),
    }, "conditions": {}, "agent": {}, "api_errors": len(errors)}

    conds = sorted({r["condition"] for r in rows})
    for c in conds:
        cr = [r for r in rows if r["condition"] == c]
        picks = [r for r in cr if r["kind"] == "pick"]
        ver = [r for r in cr if r["kind"] == "verify"]
        d = {"model": cr[0]["model"], "calls": len(cr)}

        def acc(rs):
            bp = defaultdict(list)
            for r in rs:
                bp[r["point_id"]].append(1 if r["correct"] else 0)
            m, lo, hi = boot_ci(bp)
            return {"acc": m, "ci": [lo, hi], "points": len(bp), "calls": len(rs)}

        d["pick"] = acc(picks)
        d["pick_by_group"] = {g: acc([r for r in picks if group_of(r["case_type"]) == g])
                              for g in sorted({group_of(r["case_type"]) for r in picks})}
        d["pick_by_rows"] = {}
        for n in (3, 10, 25):
            sel = [r for r in picks if r["case_type"] in (f"repeated-{n}", f"lookalike-dropdown-{n}")
                   and r["site"] == "local"]
            if sel:
                d["pick_by_rows"][str(n)] = acc(sel)
        d["pick_by_site"] = {s: acc([r for r in picks if r["site"] == s]) for s in ("local", "saucedemo")}
        d["pick_by_phrasing"] = {str(ph): acc([r for r in picks if r["phrasing"] == ph]) for ph in (0, 1, 2)}
        if ver:
            d["verify"] = acc(ver)

        # Stability: same answer on every repeat of a (point, phrasing).
        groups = defaultdict(list)
        for r in picks:
            groups[(r["point_id"], r["phrasing"])].append(r["answer"])
        multi = [v for v in groups.values() if len(v) > 1]
        d["stability"] = (sum(1 for v in multi if len(set(v)) == 1) / len(multi)) if multi else None

        lat = [r["latency_s"] for r in cr if not r["warmup"] and r["latency_s"] is not None]
        d["latency_ms"] = {"p50": pct(lat, 0.5) * 1000 if lat else None,
                           "p95": pct(lat, 0.95) * 1000 if lat else None}
        n_correct = sum(1 for r in picks if r["correct"])
        pick_cost = sum(r["cost_usd"] for r in picks)
        d["cost_usd"] = {"total": sum(r["cost_usd"] for r in cr),
                         "per_call": statistics.mean(r["cost_usd"] for r in cr),
                         "per_correct_pick": (pick_cost / n_correct) if n_correct else None}
        tin = [r["usage"].get("input_tokens", 0) for r in picks]
        d["input_tokens_per_pick"] = statistics.mean(tin) if tin else None

        if c.startswith("J"):
            bands = defaultdict(lambda: [0, 0])
            for r in picks:
                conf = r.get("confidence")
                if conf is None:
                    continue
                b = "<0.5" if conf < 0.5 else "0.5-0.8" if conf < 0.8 else "0.8-0.95" if conf < 0.95 else ">=0.95"
                bands[b][0] += 1
                bands[b][1] += 1 if r["correct"] else 0
            d["calibration"] = {b: {"n": n, "acc": k / n} for b, (n, k) in bands.items()}
        summary["conditions"][c] = d

    # Agent loop: per run totals, and per action step.
    runs = defaultdict(list)
    for r in agent:
        runs[(r["flow"], r["run_id"])].append(r)
    for flow in sorted({f for f, _ in runs}):
        fr = [v for (f, _), v in runs.items() if f == flow]
        acts = [s for v in fr for s in v if s["kind"] == "mcp_action"]
        checks = [s for v in fr for s in v if s["kind"] == "mcp_verify"]
        summary["agent"][flow] = {
            "model": fr[0][0]["model"],
            "runs": len(fr),
            "steps_per_run": statistics.mean(sum(1 for s in v if s["kind"] == "mcp_action") for v in fr),
            "checks_passed": sum(1 for s in checks if s["correct"]),
            "checks_total": len(checks),
            "cost_per_run": statistics.mean(sum(s.get("cost_usd", 0) for s in v) for v in fr),
            "seconds_per_run": statistics.mean(sum(s["latency_ms"] for s in v if s["kind"] == "mcp_action") / 1000 for v in fr),
            "step_latency_ms": {"p50": pct([s["latency_ms"] for s in acts], 0.5),
                                "p95": pct([s["latency_ms"] for s in acts], 0.95)},
            "cost_per_step": statistics.mean(s["cost_usd"] for s in acts) if acts else None,
            "tool_calls_per_step": statistics.mean(s["tool_call_count"] for s in acts) if acts else None,
            "input_tokens_per_step": statistics.mean(s["input_tokens"] for s in acts) if acts else None,
            "no_op_suspected": sum(1 for s in acts if s.get("no_op_suspected")),
        }

    os.makedirs(RESULTS, exist_ok=True)
    with open(f"{RESULTS}/summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    with open(f"{RESULTS}/misses.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["condition", "point_id", "case_type", "phrasing", "repeat", "goal_or_claim",
                    "picked", "correct_element", "n_candidates"])
        for r in rows:
            if r["correct"]:
                continue
            p = points[r["point_id"]]
            if p["kind"] == "pick":
                picked = p["candidates"][r["answer"]]
                right = p["candidates"][p["label"]]
                w.writerow([r["condition"], r["point_id"], r["case_type"], r["phrasing"], r["repeat"],
                            p["goals"][r["phrasing"]],
                            f'{picked["role"]} "{picked["name"]}" | {picked["context"]}',
                            f'{right["role"]} "{right["name"]}" | {right["context"]}',
                            len(p["candidates"])])
            else:
                w.writerow([r["condition"], r["point_id"], "verify", 0, r["repeat"],
                            f'{p["claim"]} || {p["observed"]}', r["answer"], p["label"], ""])

    make_charts(summary)
    print(json.dumps(summary, indent=2))


def make_charts(summary):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping charts")
        return
    order = [c for c in ("J", "C-S", "C-L", "J-nc", "C-S-nc") if c in summary["conditions"]]
    if not order:
        return
    ink, grid, accent = "#2c2c2c", "#e0e0e0", "#1d3f72"
    plt.rcParams.update({"font.size": 10, "axes.edgecolor": grid, "axes.labelcolor": ink,
                         "xtick.color": ink, "ytick.color": ink})

    # Accuracy by condition with CIs.
    fig, ax = plt.subplots(figsize=(7, 3.2))
    accs = [summary["conditions"][c]["pick"]["acc"] * 100 for c in order]
    los = [a - summary["conditions"][c]["pick"]["ci"][0] * 100 for a, c in zip(accs, order)]
    his = [summary["conditions"][c]["pick"]["ci"][1] * 100 - a for a, c in zip(accs, order)]
    ax.bar(order, accs, color=accent, width=0.55)
    ax.errorbar(order, accs, yerr=[los, his], fmt="none", ecolor=ink, capsize=4, lw=1)
    ax.set_ylabel("Pick accuracy (%)")
    ax.set_ylim(0, 105)
    ax.grid(axis="y", color=grid)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(f"{RESULTS}/fig_accuracy.png", dpi=160)
    plt.close(fig)

    # Headline: accuracy on hard cases against cost per correct decision.
    fig, ax = plt.subplots(figsize=(7, 3.6))
    for c in order:
        d = summary["conditions"][c]
        hard = [d["pick_by_group"][g]["acc"] for g in ("repeated", "lookalike-dropdown") if g in d["pick_by_group"]]
        cpc = d["cost_usd"]["per_correct_pick"]
        if not hard or not cpc:
            continue
        y = sum(hard) / len(hard) * 100
        ax.scatter([cpc], [y], s=60, color=accent)
        ax.annotate(c, (cpc, y), textcoords="offset points", xytext=(6, 4), color=ink)
    ax.set_xscale("log")
    ax.set_xlabel("Cost per correct pick (USD, log scale)")
    ax.set_ylabel("Accuracy on hard cases (%)")
    ax.set_ylim(0, 105)
    ax.grid(color=grid)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(f"{RESULTS}/fig_accuracy_vs_cost.png", dpi=160)
    plt.close(fig)

    # Accuracy vs number of look-alike rows.
    fig, ax = plt.subplots(figsize=(7, 3.2))
    for c in order:
        br = summary["conditions"][c]["pick_by_rows"]
        xs = [int(k) for k in sorted(br, key=int)]
        ax.plot(xs, [br[str(x)]["acc"] * 100 for x in xs], marker="o", label=c)
    ax.set_xlabel("Identical rows on the page")
    ax.set_ylabel("Pick accuracy (%)")
    ax.set_xticks([3, 10, 25])
    ax.set_ylim(0, 105)
    ax.grid(color=grid)
    ax.legend(frameon=False, ncol=len(order))
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(f"{RESULTS}/fig_rows.png", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    main()
