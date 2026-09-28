"""Analyze the ambiguity study (results/ambiguity.jsonl).

Each call falls into one outcome:

  answerable point (target present and identifiable from the text shown)
    correct       picked a correct element
    wrong         picked a different element
    over-abstain  answered "none" although the answer was knowable
  unanswerable point, target present but not identifiable from the text
    abstain       answered "none" (the right response)
    lucky-guess   picked the correct element anyway
    wrong-guess   picked a different element
  missing point (target not on the page)
    abstain       answered "none" (the right response)
    false-action  picked some element

Writes results/ambiguity_summary.json, results/ambiguity_outcomes.csv and
charts. Confidence intervals are 95% bootstrap intervals over points.
"""

import csv
import json
import os
import random
import statistics
from collections import Counter, defaultdict

from ambiguity_common import REPS

R = "results"
BOOT = 2000
ENGINES = ["jev", "haiku", "opus"]


def outcome(r, point):
    a = r["answer"]
    if point["kind"] == "missing":
        return "abstain" if a == -1 else "false-action"
    if r["answerable"]:
        return "over-abstain" if a == -1 else ("correct" if a in point["labels"] else "wrong")
    return "abstain" if a == -1 else ("lucky-guess" if a in point["labels"] else "wrong-guess")


def right(o):
    return o in ("correct", "abstain")


def boot(by_point, seed=5):
    vals = [sum(v) / len(v) for v in by_point.values()]
    if not vals:
        return None
    rng = random.Random(seed)
    stats = sorted(sum(vals[rng.randrange(len(vals))] for _ in vals) / len(vals) for _ in range(BOOT))
    return {"rate": sum(vals) / len(vals), "ci": [stats[int(0.025 * BOOT)], stats[int(0.975 * BOOT) - 1]],
            "points": len(vals)}


def rate(rows, pred):
    bp = defaultdict(list)
    for r in rows:
        bp[r["point_id"]].append(1 if pred(r) else 0)
    return boot(bp)


def auroc(scores_pos, scores_neg):
    if not scores_pos or not scores_neg:
        return None
    wins = 0.0
    for p in scores_pos:
        for n in scores_neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(scores_pos) * len(scores_neg))


def first_match(r, point):
    """For a guess on an ambiguous point: did it pick the first element in
    page order among those with the same role and name as the target?"""
    c = point["candidates"]
    t = c[point["labels"][0]] if point["labels"] else None
    if t is None or r["answer"] in (None, -1):
        return None
    group = [i for i, e in enumerate(c) if e["role"] == t["role"] and e["name"] == t["name"]]
    if r["answer"] not in group or len(group) < 2:
        return None
    return r["answer"] == group[0]


def main():
    points = {p["id"]: p for p in (json.loads(l) for l in open("data/ambiguity.jsonl"))}
    rows = [json.loads(l) for l in open(f"{R}/ambiguity.jsonl")]
    errors = [r for r in rows if r.get("error")]
    rows = [r for r in rows if not r.get("error")]
    for r in rows:
        r["outcome"] = outcome(r, points[r["point_id"]])
        r["right"] = right(r["outcome"])

    S = {"points": Counter(p["kind"] for p in points.values()), "api_errors": len(errors),
         "answerable_by_rep": {rep: sum(1 for p in points.values() if p["kind"] == "present"
                                        and any(r["answerable"] for r in rows
                                                if r["point_id"] == p["id"] and r["rep"] == rep))
                               for rep in REPS},
         "engines": {}}
    engines = [e for e in ENGINES if any(r["engine"] == e for r in rows)]
    for e in engines:
        er = [r for r in rows if r["engine"] == e]
        ans = [r for r in er if r["answerable"]]
        amb = [r for r in er if not r["answerable"] and r["kind"] == "present"]
        mis = [r for r in er if r["kind"] == "missing"]
        d = {"model": er[0]["model"], "calls": len(er)}
        d["overall_right"] = rate(er, lambda r: r["right"])
        d["answerable_correct"] = rate(ans, lambda r: r["outcome"] == "correct")
        d["answerable_over_abstain"] = rate(ans, lambda r: r["outcome"] == "over-abstain")
        d["ambiguous_abstain"] = rate(amb, lambda r: r["outcome"] == "abstain")
        d["ambiguous_silent_guess"] = rate(amb, lambda r: r["outcome"] in ("lucky-guess", "wrong-guess"))
        d["missing_abstain"] = rate(mis, lambda r: r["outcome"] == "abstain")
        fm = [first_match(r, points[r["point_id"]]) for r in amb]
        fm = [x for x in fm if x is not None]
        d["guesses_first_match_share"] = (sum(fm) / len(fm)) if fm else None
        d["by_rep"] = {}
        for rep in REPS:
            rr = [r for r in er if r["rep"] == rep]
            d["by_rep"][rep] = {
                "outcomes": Counter(r["outcome"] for r in rr),
                "right": rate(rr, lambda r: r["right"]),
            }
        # order leakage: lucky guesses with page order vs shuffled
        for rep in ("none", "none-shuffled"):
            rr = [r for r in amb if r["rep"] == rep]
            d[f"lucky_guess_rate_{rep}"] = rate(rr, lambda r: r["outcome"] == "lucky-guess")
        # confidence: does it separate right responses from wrong ones?
        conf = [r for r in er if r.get("confidence") is not None]
        d["confidence_auroc_right_vs_wrong"] = auroc([r["confidence"] for r in conf if r["right"]],
                                                     [r["confidence"] for r in conf if not r["right"]])
        picks = [r for r in conf if r["answer"] not in (None, -1)]
        d["confidence_auroc_among_picks"] = auroc(
            [r["confidence"] for r in picks if r["outcome"] == "correct"],
            [r["confidence"] for r in picks if r["outcome"] != "correct"])
        bands = defaultdict(lambda: [0, 0])
        for r in picks:
            c = r["confidence"]
            b = "<0.5" if c < 0.5 else "0.5-0.8" if c < 0.8 else "0.8-0.95" if c < 0.95 else ">=0.95"
            bands[b][0] += 1
            bands[b][1] += 1 if r["outcome"] == "correct" else 0
        d["pick_confidence_bands"] = {b: {"n": n, "correct": k / n} for b, (n, k) in sorted(bands.items())}
        # stability across repeats
        grp = defaultdict(list)
        for r in er:
            grp[(r["point_id"], r["rep"])].append(r["answer"])
        multi = [v for v in grp.values() if len(v) > 1]
        d["stability"] = (sum(1 for v in multi if len(set(v)) == 1) / len(multi)) if multi else None
        lat = [r["latency_s"] for r in er if not r["warmup"] and r["latency_s"] is not None]
        d["latency_ms_p50"] = statistics.median(lat) * 1000 if lat else None
        d["cost_total"] = sum(r["cost_usd"] for r in er)
        d["cost_per_call"] = d["cost_total"] / len(er)
        S["engines"][e] = d

    os.makedirs(R, exist_ok=True)
    with open(f"{R}/ambiguity_summary.json", "w") as f:
        json.dump(S, f, indent=2, default=dict)
    with open(f"{R}/ambiguity_outcomes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["engine", "point_id", "rep", "repeat", "kind", "answerable", "outcome", "goal",
                    "answer", "answer_text", "labels", "confidence"])
        for r in rows:
            p = points[r["point_id"]]
            a = r["answer"]
            txt = "NONE" if a == -1 else f'{p["candidates"][a]["role"]} "{p["candidates"][a]["name"]}"'
            w.writerow([r["engine"], r["point_id"], r["rep"], r["repeat"], r["kind"], r["answerable"],
                        r["outcome"], p["goal"], a, txt, p["labels"], r.get("confidence")])
    charts(S)
    print(json.dumps(S, indent=2, default=dict))


def charts(S):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping charts")
        return
    ink, grid = "#2c2c2c", "#e0e0e0"
    colors = {"correct": "#1d3f72", "abstain": "#4f8a5b", "over-abstain": "#9bbfa3",
              "lucky-guess": "#e0b24a", "wrong-guess": "#c9573c", "wrong": "#8c2d1c", "false-action": "#5a1a10"}
    order = ["correct", "abstain", "over-abstain", "lucky-guess", "wrong-guess", "wrong", "false-action"]
    engines = list(S["engines"])
    fig, axes = plt.subplots(1, len(engines), figsize=(3.4 * len(engines), 3.6), sharey=True)
    if len(engines) == 1:
        axes = [axes]
    for ax, e in zip(axes, engines):
        br = S["engines"][e]["by_rep"]
        bottoms = [0.0] * len(REPS)
        for o in order:
            vals = []
            for rep in REPS:
                c = br[rep]["outcomes"]
                tot = sum(c.values()) or 1
                vals.append(100 * c.get(o, 0) / tot)
            ax.bar(REPS, vals, bottom=bottoms, color=colors[o], label=o, width=0.7)
            bottoms = [b + v for b, v in zip(bottoms, vals)]
        ax.set_title(e, color=ink)
        ax.tick_params(axis="x", rotation=45)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    axes[0].set_ylabel("Share of responses (%)")
    axes[-1].legend(bbox_to_anchor=(1.02, 1), loc="upper left", frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(f"{R}/fig_ambiguity_outcomes.png", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    main()
