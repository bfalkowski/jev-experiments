"""Decision engines for the offline element-selection experiment.

Every engine answers the same two kinds of question from the same input:

  pick(goal, candidates)    -> index into candidates
  verify(claim, observed)   -> True / False (does the evidence support the claim?)

Jev answers through TypeSafe's typed `choice` and `noul` questions.
Constrained Claude answers through a single Messages call with structured
output: a JSON schema whose only field is the answer (an index limited to
the valid positions, or a boolean), so it cannot say anything except the
answer. (A forced tool call does the same job, but claude-opus-5-5 rejects
forced tool_choice, so structured output is used for every Claude model.)

Engines never raise on API errors; they return a result with `error` set
so a long run keeps going and the failure is visible in the results.
"""

import json
import os
import time
import urllib.error
import urllib.request

from typesafe_client import load_dotenv

load_dotenv()

# Prices per million tokens. Checked 2026-09-27:
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://docs.typesafe.ai/models ($0.042 per million input tokens, output free)
PRICES = {
    "jev-latest": {"in": 0.042, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0},
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0, "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-5": {"in": 2.0, "out": 10.0, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "cache_read": 0.20, "cache_write": 5.0},
}


def price(model, usage):
    p = PRICES[model]
    return (usage.get("input_tokens", 0) * p["in"]
            + usage.get("output_tokens", 0) * p["out"]
            + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
            + usage.get("cache_creation_input_tokens", 0) * p["cache_write"]) / 1_000_000


def candidate_line(e, with_context):
    """The exact text both Jev and Claude see for one candidate. Kept in one
    place so the two engines cannot drift apart."""
    if with_context:
        return f'{e["role"]}: "{e["name"]}" (context: "{e["context"]}")'
    return f'{e["role"]}: "{e["name"]}"'


# ------------------------------------------------------------------ Jev

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"


def _typesafe(state, questions):
    payload = json.dumps({"state": state, "model": JEV_MODEL, "questions": questions}).encode()
    req = urllib.request.Request(TYPESAFE_URL, data=payload, method="POST", headers={
        "Authorization": f"Bearer {os.environ.get('TYPESAFE_API_KEY', '')}",
        "Content-Type": "application/json",
    })
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    return body, time.perf_counter() - start


def jev_pick(goal, candidates, with_context=True):
    lines = [candidate_line(e, with_context) for e in candidates]
    state = {"goal": goal, "page_elements": lines}
    questions = {"target": {
        "type": "choice",
        "instructions": f"Which page element should be interacted with to: {goal}",
        "criteria": {str(i): line for i, line in enumerate(lines)},
    }}
    try:
        body, elapsed = _typesafe(state, questions)
        ans = body["answers"]["target"]
        usage = body.get("usage", {}) or {}
        return {"answer": int(ans["choice"]), "confidence": ans.get("confidence"),
                "latency_s": elapsed, "usage": usage, "cost_usd": price(JEV_MODEL, usage),
                "model": JEV_MODEL, "error": None}
    except Exception as e:  # noqa: BLE001 -- recorded, not swallowed
        return {"answer": None, "error": f"{type(e).__name__}: {e}", "model": JEV_MODEL,
                "latency_s": None, "usage": {}, "cost_usd": 0.0}


def jev_verify(claim, observed):
    state = {"claim": claim, "observed": observed}
    questions = {"verdict": {"type": "noul",
                             "instructions": "Does the observed evidence support the claim?"}}
    try:
        body, elapsed = _typesafe(state, questions)
        score = body["answers"]["verdict"]["noul"]
        usage = body.get("usage", {}) or {}
        return {"answer": bool(score >= 0.5), "confidence": score, "latency_s": elapsed,
                "usage": usage, "cost_usd": price(JEV_MODEL, usage), "model": JEV_MODEL, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"answer": None, "error": f"{type(e).__name__}: {e}", "model": JEV_MODEL,
                "latency_s": None, "usage": {}, "cost_usd": 0.0}


# ---------------------------------------------------------- constrained Claude

PICK_SYSTEM = (
    "You pick which element on a web page a browser test should act on. "
    "You get a goal and a numbered list of the page's interactive elements. "
    "Several elements can share the same name, so use each element's context "
    "(the text around it, such as which row it is in) to choose. "
    "Answer with the number of the one correct element."
)
VERIFY_SYSTEM = (
    "You check whether evidence observed on a web page supports a claim about it. "
    "Answer with supported set to true if the evidence supports the claim "
    "and false otherwise."
)

_client = None


def _anthropic():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic()
    return _client


# Room for any reasoning the model does before the JSON answer.
MAX_TOKENS = 2048


def _json_answer(resp):
    """Structured output: the answer is the JSON text block, constrained by the
    schema. Recorded stop_reason lets the analysis spot truncated answers."""
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    return json.loads(text)


def _usage_dict(u):
    return {k: getattr(u, k, 0) or 0 for k in
            ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}


def claude_pick(goal, candidates, model, with_context=True):
    lines = [f"{i}. {candidate_line(e, with_context)}" for i, e in enumerate(candidates)]
    schema = {
        "type": "object",
        "properties": {"index": {"type": "integer", "enum": list(range(len(candidates)))}},
        "required": ["index"],
        "additionalProperties": False,
    }
    content = f"Goal: {goal}\n\nElements:\n" + "\n".join(lines)
    try:
        start = time.perf_counter()
        resp = _anthropic().messages.create(
            model=model, max_tokens=MAX_TOKENS, system=PICK_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
        elapsed = time.perf_counter() - start
        usage = _usage_dict(resp.usage)
        return {"answer": int(_json_answer(resp)["index"]), "confidence": None, "latency_s": elapsed,
                "usage": usage, "cost_usd": price(model, usage), "model": model,
                "stop_reason": getattr(resp, "stop_reason", None), "error": None}
    except Exception as e:  # noqa: BLE001
        return {"answer": None, "error": f"{type(e).__name__}: {e}", "model": model,
                "latency_s": None, "usage": {}, "cost_usd": 0.0}


def claude_verify(claim, observed, model):
    schema = {
        "type": "object",
        "properties": {"supported": {"type": "boolean"}},
        "required": ["supported"],
        "additionalProperties": False,
    }
    content = f"Claim: {claim}\n\nObserved: {observed}"
    try:
        start = time.perf_counter()
        resp = _anthropic().messages.create(
            model=model, max_tokens=MAX_TOKENS, system=VERIFY_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
        elapsed = time.perf_counter() - start
        usage = _usage_dict(resp.usage)
        return {"answer": bool(_json_answer(resp)["supported"]), "confidence": None, "latency_s": elapsed,
                "usage": usage, "cost_usd": price(model, usage), "model": model,
                "stop_reason": getattr(resp, "stop_reason", None), "error": None}
    except Exception as e:  # noqa: BLE001
        return {"answer": None, "error": f"{type(e).__name__}: {e}", "model": model,
                "latency_s": None, "usage": {}, "cost_usd": 0.0}


# ------------------------------------------------------------ conditions

HAIKU = "claude-haiku-4-5"
OPUS = "claude-opus-5-5"

CONDITIONS = {
    # id: (description, pick_fn, verify_fn or None)
    "J": ("Jev, with row context",
          lambda g, c: jev_pick(g, c, True), jev_verify),
    "J-nc": ("Jev, no context",
             lambda g, c: jev_pick(g, c, False), None),
    "C-S": (f"Constrained Claude ({HAIKU}), with row context",
            lambda g, c: claude_pick(g, c, HAIKU, True), lambda cl, ob: claude_verify(cl, ob, HAIKU)),
    "C-S-nc": (f"Constrained Claude ({HAIKU}), no context",
               lambda g, c: claude_pick(g, c, HAIKU, False), None),
    "C-L": (f"Constrained Claude ({OPUS}), with row context",
            lambda g, c: claude_pick(g, c, OPUS, True), lambda cl, ob: claude_verify(cl, ob, OPUS)),
}


# ------------------------------------------------ ambiguity study (abstain)
#
# Same engines, but every question has an explicit way out: -1 means "none
# of these" (the element is not on the page) or "can't tell which" (several
# fit and the text shown does not separate them). Engines also report a
# confidence: Jev natively, Claude as a number in its structured answer.

ABSTAIN_RULE = (
    "If no element fits the goal, or more than one element could fit and the "
    "information shown does not tell you which one is meant, do not guess: "
    "answer -1."
)
PICK_ABSTAIN_SYSTEM = (
    "You pick which element on a web page a browser test should act on. "
    "You get a goal and a numbered list of the page's interactive elements, "
    "sometimes with text from around each element. " + ABSTAIN_RULE + " "
    "Also give your confidence, from 0 to 1, that your answer is the right response."
)


def jev_pick_abstain(goal, lines):
    criteria = {str(i): ln for i, ln in enumerate(lines)}
    criteria["none"] = ("None of the listed elements: the element is not on the page, or several "
                        "match and the information shown does not tell them apart")
    state = {"goal": goal, "page_elements": lines}
    questions = {"target": {
        "type": "choice",
        "instructions": f"Which page element should be interacted with to: {goal}. {ABSTAIN_RULE.replace('answer -1', 'answer none')}",
        "criteria": criteria,
    }}
    try:
        body, elapsed = _typesafe(state, questions)
        ans = body["answers"]["target"]
        usage = body.get("usage", {}) or {}
        choice = ans["choice"]
        return {"answer": -1 if choice == "none" else int(choice), "confidence": ans.get("confidence"),
                "latency_s": elapsed, "usage": usage, "cost_usd": price(JEV_MODEL, usage),
                "model": JEV_MODEL, "stop_reason": None, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"answer": None, "error": f"{type(e).__name__}: {e}", "model": JEV_MODEL,
                "latency_s": None, "usage": {}, "cost_usd": 0.0}


def claude_pick_abstain(goal, lines, model):
    schema = {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "enum": [-1] + list(range(len(lines)))},
            "confidence": {"type": "number"},
        },
        "required": ["index", "confidence"],
        "additionalProperties": False,
    }
    content = f"Goal: {goal}\n\nElements:\n" + "\n".join(f"{i}. {ln}" for i, ln in enumerate(lines))
    try:
        start = time.perf_counter()
        resp = _anthropic().messages.create(
            model=model, max_tokens=MAX_TOKENS, system=PICK_ABSTAIN_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
        elapsed = time.perf_counter() - start
        usage = _usage_dict(resp.usage)
        out = _json_answer(resp)
        conf = out.get("confidence")
        return {"answer": int(out["index"]),
                "confidence": None if conf is None else max(0.0, min(1.0, float(conf))),
                "latency_s": elapsed, "usage": usage, "cost_usd": price(model, usage), "model": model,
                "stop_reason": getattr(resp, "stop_reason", None), "error": None}
    except Exception as e:  # noqa: BLE001
        return {"answer": None, "error": f"{type(e).__name__}: {e}", "model": model,
                "latency_s": None, "usage": {}, "cost_usd": 0.0}


ABSTAIN_ENGINES = {
    "jev": lambda g, lines: jev_pick_abstain(g, lines),
    "haiku": lambda g, lines: claude_pick_abstain(g, lines, HAIKU),
    "opus": lambda g, lines: claude_pick_abstain(g, lines, OPUS),
}
