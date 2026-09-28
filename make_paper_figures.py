"""Make every number and figure the paper quotes, from the results files.

  python make_paper_figures.py --out <dir>

Writes <dir>/paper_numbers.json and <dir>/fig-*.png. Nothing in the paper is
typed by hand from a terminal: if a result changes, rerun this.
"""

import argparse
import json
import os
import random
from collections import Counter, defaultdict

from analyze_ambiguity import outcome

ENGINE_COLORS = {"jev": "#2a78d6", "haiku": "#eb6834", "opus": "#1baf7a", "agent": "#eda100"}
ENGINE_LABEL = {"jev": "Jev", "haiku": "Haiku 4.5", "opus": "Opus 5.5", "agent": "Opus 5.5 agent (playwright-mcp)"}
INK, MUTED, GRID = "#1d2433", "#667085", "#e3e5e8"


def jl(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def boot(by_point, seed=5, n=2000):
    vals = [sum(v) / len(v) for v in by_point.values()]
    if not vals:
        return None
    rng = random.Random(seed)
    st = sorted(sum(vals[rng.randrange(len(vals))] for _ in vals) / len(vals) for _ in range(n))
    return {"rate": sum(vals) / len(vals), "lo": st[int(0.025 * n)], "hi": st[int(0.975 * n) - 1], "points": len(vals)}


def rate(rows, pred, key="point_id"):
    bp = defaultdict(list)
    for r in rows:
        bp[r[key]].append(1 if pred(r) else 0)
    return boot(bp)


def study1():
    pts = {p["id"]: p for p in jl("data/decisions.jsonl")}
    rows = [r for r in jl("results/main.jsonl") if not r.get("error")]
    out = {}
    for c in ("J", "J-nc", "C-S", "C-S-nc"):
        cr = [r for r in rows if r["condition"] == c and r["kind"] == "pick"]
        for r in cr:
            p = pts[r["point_id"]]
            r["ok"] = r["answer"] in p.get("labels", [p["label"]])
        rep = [r for r in cr if r["case_type"].startswith("repeated") and r["site"] == "local"]
        firsts = []
        for r in rep:
            p = pts[r["point_id"]]
            t = p["candidates"][p["label"]]
            grp = [i for i, e in enumerate(p["candidates"]) if e["role"] == t["role"] and e["name"] == t["name"]]
            if r["answer"] in grp:
                firsts.append(r["answer"] == grp[0])
        lat = sorted(r["latency_s"] for r in cr if not r["warmup"])
        out[c] = {
            "calls": len(cr), "points": len({r["point_id"] for r in cr}),
            "acc": rate(cr, lambda r: r["ok"]),
            "acc_local_repeated": rate(rep, lambda r: r["ok"]),
            "first_match_share": (sum(firsts) / len(firsts)) if firsts else None,
            "latency_p50_ms": median_ms(lat),
            "cost_total": sum(r["cost_usd"] for r in cr),
        }
        misses = [r for r in cr if not r["ok"]]
        out[c]["misses"] = Counter(r["point_id"] for r in misses).most_common(5)
    return out


def study2():
    pts = {p["id"]: p for p in jl("data/ambiguity.jsonl")}
    rows = [r for r in jl("results/ambiguity.jsonl") if not r.get("error")]
    for r in rows:
        r["outcome"] = outcome(r, pts[r["point_id"]])
    out = {"points": dict(Counter(p["kind"] for p in pts.values()))}
    thresholds = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.97, 0.99]
    for e in ("jev", "haiku", "opus"):
        er = [r for r in rows if r["engine"] == e]
        ans = [r for r in er if r["answerable"]]
        amb = [r for r in er if not r["answerable"] and r["kind"] == "present"]
        mis = [r for r in er if r["kind"] == "missing"]
        d = {
            "calls": len(er),
            "answerable_correct": rate(ans, lambda r: r["outcome"] == "correct"),
            "answerable_over_abstain": rate(ans, lambda r: r["outcome"] == "over-abstain"),
            "ambiguous_abstain": rate(amb, lambda r: r["outcome"] == "abstain"),
            "ambiguous_wrong_guess": rate(amb, lambda r: r["outcome"] == "wrong-guess"),
            "missing_abstain": rate(mis, lambda r: r["outcome"] == "abstain"),
            "missing_false_action": rate(mis, lambda r: r["outcome"] == "false-action"),
            "guess_rate_page_order": rate([r for r in amb if r["rep"] == "none"],
                                          lambda r: r["outcome"] in ("lucky-guess", "wrong-guess")),
            "guess_rate_shuffled": rate([r for r in amb if r["rep"] == "none-shuffled"],
                                        lambda r: r["outcome"] in ("lucky-guess", "wrong-guess")),
            "lucky_page_order": rate([r for r in amb if r["rep"] == "none"], lambda r: r["outcome"] == "lucky-guess"),
            "lucky_shuffled": rate([r for r in amb if r["rep"] == "none-shuffled"],
                                   lambda r: r["outcome"] == "lucky-guess"),
            "cost_total": sum(r["cost_usd"] for r in er),
            "latency_p50_ms": median_ms([r["latency_s"] for r in er if not r["warmup"] and r["latency_s"]]),
        }
        # Acting only above a confidence threshold: of the picks that were not
        # correct (wrong guesses, false actions), how many would be blocked,
        # and how many correct picks would be lost?
        picks = [r for r in er if r["answer"] not in (None, -1) and r.get("confidence") is not None]
        good = [r for r in picks if r["outcome"] == "correct"]
        bad = [r for r in picks if r["outcome"] in ("wrong-guess", "false-action", "wrong")]
        lucky = [r for r in picks if r["outcome"] == "lucky-guess"]
        d["threshold"] = [{"t": t,
                           "bad_blocked": sum(1 for r in bad if r["confidence"] < t) / len(bad) if bad else None,
                           "good_kept": sum(1 for r in good if r["confidence"] >= t) / len(good) if good else None,
                           "lucky_blocked": sum(1 for r in lucky if r["confidence"] < t) / len(lucky) if lucky else None}
                          for t in thresholds]
        d["n_picks"] = {"correct": len(good), "wrong": len(bad), "lucky": len(lucky)}
        by_rep = {}
        for rep in ("none", "none-shuffled", "parent", "grand", "wide"):
            by_rep[rep] = dict(Counter(r["outcome"] for r in er if r["rep"] == rep))
        d["by_rep"] = by_rep
        out[e] = d
    out["answerable_by_rep"] = {rep: sorted({r["point_id"] for r in rows if r["rep"] == rep and r["answerable"]})
                                for rep in ("none", "none-shuffled", "parent", "grand", "wide")}
    out["answerable_by_rep"] = {k: len(v) for k, v in out["answerable_by_rep"].items()}
    return out


def study3():
    rows = [r for r in jl("results/live.jsonl") if not r.get("error") and r["task_id"] != "P-status-1007"]
    out = {}
    for e in ("jev", "haiku", "opus", "agent"):
        er = [r for r in rows if r["engine"] == e]
        if not er:
            continue
        by_kind = defaultdict(Counter)
        for r in er:
            by_kind[r["kind"]][r["outcome"]] += 1
        out[e] = {"runs": len(er), "outcomes": dict(Counter(r["outcome"] for r in er)),
                  "by_kind": {k: dict(v) for k, v in by_kind.items()},
                  "cost_total": sum(r.get("cost_usd") or 0 for r in er),
                  "cost_per_run": sum(r.get("cost_usd") or 0 for r in er) / len(er),
                  "latency_p50_s": (median_ms([r["latency_s"] for r in er if r.get("latency_s")]) or 0) / 1000,
                  "tool_calls_mean": (sum(r.get("tool_call_count") or 0 for r in er) / len(er)) if e == "agent" else None,
                  "wrong_or_false": [(r["task_id"], r["outcome"], (r.get("acted_on") or {}).get("name"),
                                      [ev.get("type") for ev in r.get("events", [])]) for r in er
                                     if r["outcome"] in ("wrong-action", "false-action")]}
    return out


def median_ms(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return (xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2) * 1000


def style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)


def bars(ax, groups, series, values, errs=None, fmt="{:.0f}%"):
    import numpy as np
    x = np.arange(len(groups))
    w = 0.8 / len(series)
    for i, s in enumerate(series):
        vals = [values[s][g] for g in groups]
        xs = x - 0.4 + w / 2 + i * w
        ax.bar(xs, vals, w - 0.03, color=ENGINE_COLORS[s], label=ENGINE_LABEL[s], zorder=2)
        if errs:
            lo = [vals[j] - errs[s][g][0] for j, g in enumerate(groups)]
            hi = [errs[s][g][1] - vals[j] for j, g in enumerate(groups)]
            ax.errorbar(xs, vals, yerr=[lo, hi], fmt="none", ecolor=INK, elinewidth=0.9, capsize=2.5, zorder=3)
        tops = [errs[s][g][1] if errs else v for g, v in zip(groups, vals)]
        for xx, v, top in zip(xs, vals, tops):
            ax.text(xx, max(v, top) + 2, fmt.format(v), ha="center", va="bottom", fontsize=8, color=INK)
    ax.set_xticks(x)
    ax.set_xticklabels(groups, fontsize=9, color=INK)


def figures(s1, s2, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9})

    # Figure 1: Study 1 accuracy with and without row context.
    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    groups = ["With row context", "Names only"]
    vals = {"jev": {groups[0]: s1["J"]["acc"]["rate"] * 100, groups[1]: s1["J-nc"]["acc"]["rate"] * 100},
            "haiku": {groups[0]: s1["C-S"]["acc"]["rate"] * 100, groups[1]: s1["C-S-nc"]["acc"]["rate"] * 100}}
    errs = {"jev": {groups[0]: (s1["J"]["acc"]["lo"] * 100, s1["J"]["acc"]["hi"] * 100),
                    groups[1]: (s1["J-nc"]["acc"]["lo"] * 100, s1["J-nc"]["acc"]["hi"] * 100)},
            "haiku": {groups[0]: (s1["C-S"]["acc"]["lo"] * 100, s1["C-S"]["acc"]["hi"] * 100),
                      groups[1]: (s1["C-S-nc"]["acc"]["lo"] * 100, s1["C-S-nc"]["acc"]["hi"] * 100)}}
    bars(ax, groups, ["jev", "haiku"], vals, errs)
    ax.set_ylim(0, 115)
    ax.set_ylabel("Correct picks (%)", color=MUTED)
    style(ax)
    ax.legend(frameon=False, fontsize=8, loc="upper right")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "fig-study1-accuracy.png"), dpi=200)
    plt.close(fig)

    # Figure 2: Study 2, does it refuse when it should?
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    groups = ["Answerable:\ncorrect pick", "Target missing:\nrefused", "Can't tell which:\nrefused"]
    keys = ["answerable_correct", "missing_abstain", "ambiguous_abstain"]
    engines = ["jev", "haiku", "opus"]
    vals = {e: {g: s2[e][k]["rate"] * 100 for g, k in zip(groups, keys)} for e in engines}
    errs = {e: {g: (s2[e][k]["lo"] * 100, s2[e][k]["hi"] * 100) for g, k in zip(groups, keys)} for e in engines}
    bars(ax, groups, engines, vals, errs)
    ax.set_ylim(0, 118)
    ax.set_ylabel("Share of responses (%)", color=MUTED)
    style(ax)
    ax.legend(frameon=False, fontsize=8, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.12))
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "fig-study2-refusal.png"), dpi=200)
    plt.close(fig)

    # Figure 3: guessing with page order vs shuffled order.
    fig, ax = plt.subplots(figsize=(6.4, 2.9))
    groups = ["Page order", "Shuffled order"]
    vals = {e: {groups[0]: s2[e]["guess_rate_page_order"]["rate"] * 100,
                groups[1]: s2[e]["guess_rate_shuffled"]["rate"] * 100} for e in engines}
    bars(ax, groups, engines, vals)
    ax.set_ylim(0, 60)
    ax.set_ylabel("Guessed instead of refusing (%)", color=MUTED)
    style(ax)
    ax.legend(frameon=False, fontsize=8, ncol=3, loc="upper right")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "fig-study2-order.png"), dpi=200)
    plt.close(fig)

    # Figure 4: acting only above a confidence threshold (two panels, same % scale).
    fig, axes = plt.subplots(1, 2, figsize=(6.8, 2.9), sharey=True)
    for e in engines:
        ts = [p["t"] for p in s2[e]["threshold"]]
        if s2[e]["n_picks"]["wrong"]:
            axes[0].plot(ts, [p["bad_blocked"] * 100 for p in s2[e]["threshold"]], color=ENGINE_COLORS[e], lw=2,
                         marker="o", ms=4, label=f'{ENGINE_LABEL[e]} ({s2[e]["n_picks"]["wrong"]} wrong picks)')
        axes[1].plot(ts, [p["good_kept"] * 100 for p in s2[e]["threshold"]], color=ENGINE_COLORS[e], lw=2,
                     marker="o", ms=4, label=ENGINE_LABEL[e])
    axes[0].set_title("Wrong picks blocked", fontsize=9, color=INK)
    axes[1].set_title("Correct picks kept", fontsize=9, color=INK)
    for ax in axes:
        ax.set_xlabel("Act only if confidence ≥", color=MUTED)
        ax.set_ylim(-5, 108)
        style(ax)
    axes[0].set_ylabel("%", color=MUTED)
    axes[0].legend(frameon=False, fontsize=7.5, loc="lower right")
    axes[1].legend(frameon=False, fontsize=7.5, loc="lower left")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "fig-study2-threshold.png"), dpi=200)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    s1, s2, s3 = study1(), study2(), study3()
    with open(os.path.join(args.out, "paper_numbers.json"), "w") as f:
        json.dump({"study1": s1, "study2": s2, "study3": s3}, f, indent=2)
    figures(s1, s2, args.out)
    print(json.dumps({"study1": s1, "study2": s2, "study3": s3}, indent=1)[:200])


if __name__ == "__main__":
    main()
