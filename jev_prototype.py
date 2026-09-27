"""
Weekend prototype: testing TypeSafe/Jev's System One API.

Two tests:
  1. Basic smoke test — one call, all three primitives (Noul/Choice/Score),
     against a generic support-ticket-style state. Just to see real shapes
     and real latency (measured here, not taken from their blog post).
  2. Claim-verification test modeled on TypeSafe's own "citation_check"
     cookbook pattern, but pointed at a toy version of the actual problem:
     does an observed UI state (e.g. a rendered dropdown) match a claim
     about what the spec says it should be. This is the pattern worth
     validating before trusting it for real UI-correctness checks.

Usage:
    export TYPESAFE_API_KEY=sk-...
    python jev_prototype.py
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"


def _load_dotenv(path=".env"):
    """Tiny .env loader so we don't need python-dotenv as a dependency.
    Does nothing if the file isn't there or the var is already set."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def ask_typesafe(state, questions):
    """POST a state + typed questions to the System One API. Returns
    (parsed_json_response, elapsed_seconds)."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        sys.exit("Set TYPESAFE_API_KEY in your environment first.")

    payload = json.dumps({
        "state": state,
        "model": MODEL,
        "questions": questions,
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"HTTP {e.code} from TypeSafe API:\n{detail}")
    elapsed = time.perf_counter() - start

    return body, elapsed


def print_result(label, response, elapsed):
    print(f"\n--- {label} ---")
    print(f"latency: {elapsed*1000:.0f} ms (measured here, not vendor-claimed)")
    print(f"usage:   {response.get('usage')}")
    for qid, ans in response.get("answers", {}).items():
        print(f"  [{qid}] {json.dumps(ans, indent=None)}")


def test_1_basic_smoke():
    """One call, three question types, against a support-ticket state."""
    state = {
        "ticket_text": (
            "URGENT — our production checkout page is throwing errors for "
            "every customer since this morning. We are losing sales every "
            "minute this stays broken."
        ),
    }

    questions = {
        "is_urgent": {
            "type": "noul",
            "instructions": "Does this ticket convey urgency requiring immediate action?",
        },
        "route_to": {
            "type": "choice",
            "instructions": "Which team should handle this ticket?",
            "criteria": {
                "billing": "Payment or invoicing issues",
                "technical": "Bugs, outages, or broken functionality",
                "sales": "Pre-purchase questions",
            },
        },
        "frustration_level": {
            "type": "score",
            "instructions": "How frustrated does the customer sound?",
            "criteria": ["Calm", "Frustrated", "Very angry"],
        },
    }

    response, elapsed = ask_typesafe(state, questions)
    print_result("Test 1: basic smoke test", response, elapsed)


def test_2_claim_verification():
    """
    Modeled on TypeSafe's citation_check cookbook: state = {claim, evidence},
    single Choice question (supports/contradicts/says_nothing), then apply
    the same 0.8 auto-accept threshold they use, routing anything below it
    to human review instead of trusting it blindly.

    Toy example standing in for a real UI-correctness check: does the
    rendered dropdown's option set match what the record-type spec says
    it should contain.
    """
    cases = [
        {
            "claim": "The Status dropdown should offer exactly these options: "
                     "Open, In Progress, Closed (per the record type's status field).",
            "observed": "Rendered dropdown options found in the a11y tree: "
                        "['Open', 'In Progress', 'Closed'].",
        },
        {
            "claim": "The Status dropdown should offer exactly these options: "
                     "Open, In Progress, Closed (per the record type's status field).",
            "observed": "Rendered dropdown options found in the a11y tree: "
                        "['Open', 'Closed'].",  # missing "In Progress"
        },
        {
            "claim": "The Status dropdown should offer exactly these options: "
                     "Open, In Progress, Closed (per the record type's status field).",
            "observed": "Rendered dropdown options found in the a11y tree: "
                        "['Open', 'In Progress', 'Closed', 'Archived'].",  # extra option
        },
    ]

    AUTO_ACCEPT = 0.8

    for i, case in enumerate(cases, start=1):
        state = {"claim": case["claim"], "evidence": case["observed"]}
        questions = {
            "verdict": {
                "type": "choice",
                "instructions": "Does the evidence support, contradict, or say nothing about the claim?",
                "criteria": {
                    "supports": "The evidence shows the claim is true.",
                    "contradicts": "The evidence shows the claim is false "
                                   "(e.g. missing or extra options).",
                    "says_nothing": "The evidence does not address the claim either way.",
                },
            }
        }

        response, elapsed = ask_typesafe(state, questions)
        answer = response["answers"]["verdict"]
        conf = answer.get("confidence", 0)
        routing = "AUTO-ACCEPT" if conf >= AUTO_ACCEPT else "-> ROUTE TO HUMAN REVIEW"

        print(f"\n--- Test 2, case {i} ---")
        print(f"observed: {case['observed']}")
        print(f"verdict:  {answer['choice']}  (confidence {conf:.2f})  {routing}")
        print(f"latency:  {elapsed*1000:.0f} ms")


if __name__ == "__main__":
    test_1_basic_smoke()
    test_2_claim_verification()
