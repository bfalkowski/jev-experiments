"""Run the ambiguity study: every engine answers every decision point under
every context level, with an explicit way to abstain.

  python run_ambiguity.py --dry-run
  python run_ambiguity.py --pilot --max-usd 0.30
  python run_ambiguity.py --engines jev,haiku --max-usd 1.50
  python run_ambiguity.py --engines opus --max-usd 4

Appends one line per call to results/ambiguity.jsonl (or
results/ambiguity_pilot.jsonl with --pilot) and resumes without repeating
finished calls.
"""

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone

from ambiguity_common import REPS, answerable, view
from engines import ABSTAIN_ENGINES, PRICES, HAIKU, OPUS

DATA = "data/ambiguity.jsonl"
MODEL_OF = {"jev": "jev-latest", "haiku": HAIKU, "opus": OPUS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default="jev,haiku,opus")
    ap.add_argument("--reps", default=",".join(REPS))
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--pilot", action="store_true", help="8 points, 1 repeat")
    ap.add_argument("--max-usd", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    reps = [r.strip() for r in args.reps.split(",") if r.strip()]
    for e in engines:
        if e not in ABSTAIN_ENGINES:
            sys.exit(f"unknown engine {e}")
    points = [json.loads(line) for line in open(DATA)]
    repeats = args.repeats
    out = "results/ambiguity.jsonl"
    if args.pilot:
        points = points[::5][:8]
        repeats = 1
        out = "results/ambiguity_pilot.jsonl"
    by_id = {p["id"]: p for p in points}

    jobs = [(e, p["id"], r, k) for e in engines for p in points for r in reps for k in range(repeats)]
    done = set()
    if os.path.exists(out):
        for line in open(out):
            row = json.loads(line)
            if row.get("error") is None:
                done.add((row["engine"], row["point_id"], row["rep"], row["repeat"]))
    todo = [j for j in jobs if j not in done]
    random.Random(args.seed).shuffle(todo)

    est = {}
    for e, pid, r, _ in todo:
        lines, _ = view(by_id[pid], r)
        chars = len(by_id[pid]["goal"]) + sum(len(x) + 6 for x in lines)
        tin = chars / 3.5 + (0 if e == "jev" else 500)
        tout = 0 if e == "jev" else 25
        pr = PRICES[MODEL_OF[e]]
        n, usd = est.get(e, (0, 0.0))
        est[e] = (n + 1, usd + (tin * pr["in"] + tout * pr["out"]) / 1e6)
    print(f"{len(jobs)} jobs, {len(done)} done, {len(todo)} to run")
    for e in engines:
        n, usd = est.get(e, (0, 0.0))
        print(f"  {e:<6} {n:>5} calls  est. ${usd:.2f}")
    print(f"  total est. ${sum(v[1] for v in est.values()):.2f}")
    if args.dry_run:
        return
    if args.max_usd is None:
        sys.exit("refusing to run without --max-usd")

    try:
        rev = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        rev = None
    os.makedirs("results", exist_ok=True)
    spent, warmed = 0.0, set()
    with open(out, "a") as f:
        for i, (e, pid, r, k) in enumerate(todo, 1):
            if spent >= args.max_usd:
                print(f"budget cap reached: ${spent:.4f}")
                break
            p = by_id[pid]
            lines, order = view(p, r)
            res = ABSTAIN_ENGINES[e](p["goal"], lines)
            shown = res["answer"]
            orig = None if shown is None else (-1 if shown == -1 else order[shown])
            row = {"engine": e, "point_id": pid, "rep": r, "repeat": k, "site": p["site"],
                   "kind": p["kind"], "answerable": answerable(p, r), "labels": p["labels"],
                   "answer_shown": shown, "answer": orig, "n_candidates": len(p["candidates"]),
                   "warmup": e not in warmed, "git_rev": rev,
                   "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   **{kk: v for kk, v in res.items() if kk != "answer"}}
            warmed.add(e)
            spent += res["cost_usd"] or 0.0
            f.write(json.dumps(row) + "\n")
            f.flush()
            tag = "ERR" if orig is None else ("none" if orig == -1 else ("hit" if orig in p["labels"] else "pick"))
            print(f"[{i}/{len(todo)}] ${spent:.4f} {e:<5} {r:<13} {tag:<4} {pid}"
                  + (f"  {res['error'][:80]}" if res.get("error") else ""))
    print(f"spent this run: ${spent:.4f}")


if __name__ == "__main__":
    main()
