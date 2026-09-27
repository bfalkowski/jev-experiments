"""Run the offline element-selection experiment on recorded decision points.

Every condition answers every decision point (each goal phrasing, several
repeats). Jobs are shuffled together so no engine systematically runs
earlier or later than another. Results are appended to a JSONL file one
line per call, so a stopped run resumes without paying for finished calls.

Examples:
  python run_offline.py --dry-run                         # estimate cost, no API calls
  python run_offline.py --stage pilot --max-usd 0.50      # ~10 points, 1 repeat
  python run_offline.py --conditions J,J-nc,C-S,C-S-nc --max-usd 3
  python run_offline.py --conditions C-L --max-usd 6
"""

import argparse
import json
import os
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

from engines import CONDITIONS, PRICES, candidate_line

DATA = "data/decisions.jsonl"


def load_points():
    with open(DATA) as f:
        return [json.loads(line) for line in f]


def pilot_subset(points):
    picks = [p for p in points if p["kind"] == "pick"]
    verifies = [p for p in points if p["kind"] == "verify"]
    step = max(1, len(picks) // 10)
    return picks[::step][:10] + verifies[:2]


def build_jobs(points, conditions, repeats, phrasings):
    jobs = []
    for cond in conditions:
        _, pick_fn, verify_fn = CONDITIONS[cond]
        for p in points:
            if p["kind"] == "pick":
                if p["label"] is None:
                    continue  # enumeration miss: no engine can be right; reported separately
                for ph in range(min(phrasings, len(p["goals"]))):
                    for r in range(repeats):
                        jobs.append((cond, p["id"], ph, r))
            elif verify_fn is not None:
                for r in range(repeats):
                    jobs.append((cond, p["id"], 0, r))
    return jobs


def estimate(points_by_id, jobs):
    """Rough token estimate: about 3.5 characters per token, plus the fixed
    system prompt and tool definition for Claude calls."""
    per_cond = defaultdict(lambda: [0, 0.0])
    for cond, pid, ph, _ in jobs:
        p = points_by_id[pid]
        if p["kind"] == "pick":
            ctx = not cond.endswith("-nc")
            chars = len(p["goals"][ph]) + sum(len(candidate_line(e, ctx)) + 6 for e in p["candidates"])
        else:
            chars = len(p["claim"]) + len(p["observed"])
        is_jev = cond.startswith("J")
        tokens_in = chars / 3.5 + (0 if is_jev else 450)
        tokens_out = 0 if is_jev else 40
        model = "jev-latest" if is_jev else CONDITIONS[cond][0].split("(")[1].split(")")[0]
        pr = PRICES[model]
        per_cond[cond][0] += 1
        per_cond[cond][1] += (tokens_in * pr["in"] + tokens_out * pr["out"]) / 1_000_000
    return per_cond


def git_rev():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--stage", choices=["pilot", "main"], default="main")
    ap.add_argument("--repeats", type=int, default=None, help="default: 1 for pilot, 3 for main")
    ap.add_argument("--phrasings", type=int, default=3)
    ap.add_argument("--max-usd", type=float, required=False, default=None,
                    help="stop once spend in THIS invocation reaches this amount")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=None, help="default: results/<stage>.jsonl")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    for c in conditions:
        if c not in CONDITIONS:
            sys.exit(f"unknown condition {c}; choose from {list(CONDITIONS)}")
    repeats = args.repeats or (1 if args.stage == "pilot" else 3)
    out = args.out or f"results/{args.stage}.jsonl"

    points = load_points()
    if args.stage == "pilot":
        points = pilot_subset(points)
    by_id = {p["id"]: p for p in points}
    jobs = build_jobs(points, conditions, repeats, args.phrasings)

    done = set()
    if os.path.exists(out):
        with open(out) as f:
            for line in f:
                r = json.loads(line)
                if r.get("error") is None:
                    done.add((r["condition"], r["point_id"], r["phrasing"], r["repeat"]))
    todo = [j for j in jobs if j not in done]
    random.Random(args.seed).shuffle(todo)

    est = estimate(by_id, todo)
    print(f"{len(jobs)} jobs in plan, {len(done)} already done, {len(todo)} to run")
    total_est = 0.0
    for cond in conditions:
        n, usd = est.get(cond, (0, 0.0))
        total_est += usd
        print(f"  {cond:<8} {n:>5} calls   est. ${usd:6.2f}   {CONDITIONS[cond][0]}")
    print(f"  total est. ${total_est:.2f}")
    if args.dry_run:
        return
    if args.max_usd is None:
        sys.exit("refusing to run without --max-usd")

    os.makedirs(os.path.dirname(out), exist_ok=True)
    rev = git_rev()
    spent = 0.0
    warmed = set()
    with open(out, "a") as f:
        for i, (cond, pid, ph, rep) in enumerate(todo, 1):
            if spent >= args.max_usd:
                print(f"\nbudget cap reached: ${spent:.4f} >= ${args.max_usd}")
                break
            p = by_id[pid]
            _, pick_fn, verify_fn = CONDITIONS[cond]
            if p["kind"] == "pick":
                res = pick_fn(p["goals"][ph], p["candidates"])
                correct = None if res["answer"] is None else res["answer"] in p.get("labels", [p["label"]])
            else:
                res = verify_fn(p["claim"], p["observed"])
                correct = None if res["answer"] is None else res["answer"] == p["label"]
            row = {
                "condition": cond, "point_id": pid, "kind": p["kind"], "case_type": p["case_type"],
                "site": p["site"], "phrasing": ph, "repeat": rep,
                "label": p["label"], "labels": p.get("labels"), "correct": correct,
                "n_candidates": len(p.get("candidates", [])),
                "warmup": cond not in warmed,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "git_rev": rev, **res,
            }
            warmed.add(cond)
            spent += res["cost_usd"] or 0.0
            f.write(json.dumps(row) + "\n")
            f.flush()
            mark = "ok " if correct else ("ERR" if correct is None else "MISS")
            print(f"[{i}/{len(todo)}] ${spent:.4f} {cond:<7} {mark} {pid} ph{ph} r{rep}"
                  + (f"  {res['error'][:80]}" if res.get("error") else ""))
    print(f"\nspent this run: ${spent:.4f}")


if __name__ == "__main__":
    main()
