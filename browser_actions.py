"""Shared Playwright + Jev helpers, extracted so both the CLI script
(jev_browser_action.py) and the web dashboard (dashboard.py) drive the
exact same logic -- no duplicated, driftable copies of the picking
and verification code."""

from typesafe_client import ask_typesafe

INTERACTIVE_ROLES = ["textbox", "button", "checkbox", "link", "combobox"]

# docs.typesafe.ai/models, checked 2026-09-20: "Charged per input token.
# Output tokens are free." $0.042 per million input tokens.
JEV_PRICE_PER_MTOK_INPUT = 0.042


def _usage_cost(response):
    """Pulls token usage out of a TypeSafe response and prices it, so Jev
    calls are directly cost-comparable to the Claude+playwright-mcp engine
    instead of only being compared on latency."""
    usage = response.get("usage", {}) or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cost_usd = round((input_tokens / 1_000_000) * JEV_PRICE_PER_MTOK_INPUT, 8)
    return input_tokens, output_tokens, cost_usd


def get_context_snippet(item, max_len=120):
    """Grab a short snippet of text from a couple ancestor levels up, so
    elements with identical accessible names (repeated row buttons,
    repeated dropdown labels) can still be told apart by what's around
    them (e.g. the customer name in the same row)."""
    try:
        text = item.evaluate(
            """
            el => {
                let node = el;
                for (let i = 0; i < 2 && node.parentElement; i++) {
                    node = node.parentElement;
                }
                return node.innerText || '';
            }
            """
        )
    except Exception:
        text = ""
    text = " ".join((text or "").split())
    return text[:max_len]


def get_associated_label_text(item):
    """Covers a common pattern the plain aria-label check misses: an
    <input>/<select> named only by a wrapping <label> or a
    <label for="id">. Checkboxes almost always work this way."""
    try:
        return item.evaluate(
            """
            el => {
                const wrapping = el.closest('label');
                if (wrapping && wrapping.innerText.trim()) return wrapping.innerText;
                if (el.id) {
                    const lbl = document.querySelector(`label[for="${el.id}"]`);
                    if (lbl) return lbl.innerText;
                }
                return '';
            }
            """
        )
    except Exception:
        return ""


def get_interactive_elements(page):
    """Enumerate candidate interactive elements by ARIA role. Real
    identity is (role, nth) -- exact DOM position -- never the name,
    since names collide constantly on real pages (grids, repeated row
    actions). Name + context snippet are just what gets shown to Jev
    to help it choose."""
    elements = []
    for role in INTERACTIVE_ROLES:
        locator = page.get_by_role(role)
        try:
            count = locator.count()
        except Exception:
            continue
        for i in range(count):
            item = locator.nth(i)
            name = None
            for getter in (
                lambda: item.get_attribute("aria-label"),
                lambda: get_associated_label_text(item),
                lambda: item.get_attribute("placeholder"),
                lambda: item.inner_text(timeout=500),
                lambda: item.get_attribute("value"),
            ):
                try:
                    val = getter()
                except Exception:
                    val = None
                if val and val.strip():
                    name = val.strip()
                    break
            if not name:
                continue
            elements.append({
                "role": role,
                "nth": i,
                "name": name,
                "context": get_context_snippet(item),
            })
    return elements


def jev_pick_element(goal, elements):
    """Ask Jev which candidate element matches the goal. Returns
    (element_dict, confidence, elapsed_seconds, (input_tokens, output_tokens, cost_usd))."""
    criteria = {
        str(i): f'{e["role"]}: "{e["name"]}" (context: "{e["context"]}")'
        for i, e in enumerate(elements)
    }
    state = {
        "goal": goal,
        "page_elements": [
            f'{e["role"]}: "{e["name"]}" (context: "{e["context"]}")' for e in elements
        ],
    }
    questions = {
        "target": {
            "type": "choice",
            "instructions": f"Which page element should be interacted with to: {goal}",
            "criteria": criteria,
        }
    }
    response, elapsed = ask_typesafe(state, questions)
    answer = response["answers"]["target"]
    idx = int(answer["choice"])
    return elements[idx], answer["confidence"], elapsed, _usage_cost(response)


def act(page, element, action, value=None):
    """Act by exact (role, nth) position -- never by name, since names
    aren't unique identity."""
    locator = page.get_by_role(element["role"]).nth(element["nth"])
    if action == "fill":
        locator.fill(value)
    elif action == "click":
        locator.click()
        # A click can trigger navigation (e.g. opening the cart page).
        # Without this, a ground-truth check run right after can read
        # the OLD page's DOM before the new one has loaded -- that's
        # what produced a false "WRONG" on the cart-contents check.
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
    elif action == "check":
        locator.check()
    elif action == "select":
        locator.select_option(value)


def jev_verify(claim, observed):
    """A Noul verification check, separate from element-picking. Returns
    (noul_score, elapsed_seconds, (input_tokens, output_tokens, cost_usd))."""
    state = {"claim": claim, "observed": observed}
    questions = {"verdict": {
        "type": "noul",
        "instructions": "Does the observed evidence support the claim?",
    }}
    response, elapsed = ask_typesafe(state, questions)
    return response["answers"]["verdict"]["noul"], elapsed, _usage_cost(response)


def count_buttons_named_like(page, substring):
    return len([e for e in get_interactive_elements(page)
                if e["role"] == "button" and substring.lower() in e["name"].lower()])
