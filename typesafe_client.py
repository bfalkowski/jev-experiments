"""Small shared client for the TypeSafe System One API, factored out of
jev_prototype.py so jev_browser_action.py can reuse it."""

import os
import sys
import time
import json
import urllib.request
import urllib.error

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"


def load_dotenv(path=".env"):
    """Tiny .env loader, no python-dotenv dependency needed."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_dotenv()


def ask_typesafe(state, questions):
    """POST a state + typed questions to the System One API. Returns
    (parsed_json_response, elapsed_seconds)."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        sys.exit("Set TYPESAFE_API_KEY in your environment (.env) first.")

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
