"""Shared definitions for the ambiguity study: how each representation of
the candidate list is built, and whether a point is answerable under it."""

import random

REPS = ["none", "none-shuffled", "parent", "grand", "wide"]


def line(e, rep):
    if rep in ("none", "none-shuffled"):
        return f'{e["role"]}: "{e["name"]}"'
    return f'{e["role"]}: "{e["name"]}" (context: "{e["contexts"][rep]}")'


def view(point, rep):
    """Return (lines, order) where order[i] is the original index shown at
    position i. Only 'none-shuffled' changes the order (seeded by point id)."""
    order = list(range(len(point["candidates"])))
    if rep == "none-shuffled":
        random.Random(point["id"]).shuffle(order)
    return [line(point["candidates"][i], rep) for i in order], order


def answerable(point, rep):
    """True if the correct element can be told apart from look-alikes using
    only the text shown under this representation. Missing targets are never
    answerable. List order is ignored on purpose: an answer that relies on
    order is counted as a guess, and the none vs none-shuffled comparison
    measures how much order helps."""
    if point["kind"] == "missing":
        return False
    cands = point["candidates"]
    labels = set(point["labels"])
    t = cands[point["labels"][0]]
    group = [i for i, e in enumerate(cands) if e["role"] == t["role"] and e["name"] == t["name"]]
    if set(group) <= labels:
        return True
    key = (point.get("key") or "").lower()
    if not key:
        return False
    in_target = any(key in line(cands[i], rep).lower() for i in labels)
    in_other = any(key in line(cands[i], rep).lower() for i in group if i not in labels)
    return in_target and not in_other
