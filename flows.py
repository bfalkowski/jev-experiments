"""Flow definitions as generators that yield structured step results,
so the CLI script and the web dashboard can both drive them and render
the results however fits (terminal text vs. HTML cards) without
duplicating the actual flow logic."""

import base64

from browser_actions import (
    get_interactive_elements,
    jev_pick_element,
    act,
    jev_verify,
    count_buttons_named_like,
)


def _screenshot_b64(page):
    """A screenshot after each step, for the dashboard's embedded
    split-screen view -- lets you watch what happened without a separate
    OS browser window. Best-effort: a screenshot failure shouldn't take
    down the whole run."""
    try:
        png_bytes = page.screenshot(type="png")
        return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    except Exception:
        return None


def action_step(step_num, description, page, goal, act_type, value=None,
                 ground_truth_fn=None, ground_truth_expected=None):
    """Run one 'pick an element and act on it' step, yield a structured
    result. ground_truth_fn, if given, is called AFTER acting and
    should return the actual observed value to compare against
    ground_truth_expected -- this is what makes 'correct pick?' a real
    check instead of just trusting the confidence score."""
    elements = get_interactive_elements(page)
    el, confidence, latency, (input_tokens, output_tokens, cost_usd) = jev_pick_element(goal, elements)
    act(page, el, act_type, value)

    result = {
        "step": step_num,
        "kind": "action",
        "description": description,
        "picked": {"role": el["role"], "name": el["name"], "context": el["context"]},
        "confidence": confidence,
        "latency_ms": round(latency * 1000),
        "ground_truth": None,
        "screenshot": _screenshot_b64(page),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }
    if ground_truth_fn is not None:
        actual = ground_truth_fn(page)
        result["ground_truth"] = {
            "expected": ground_truth_expected,
            "actual": actual,
            "correct": actual == ground_truth_expected,
        }
    return result


def verify_step(step_num, description, claim, observed, page=None):
    noul, latency, (input_tokens, output_tokens, cost_usd) = jev_verify(claim, observed)
    return {
        "step": step_num,
        "kind": "verify",
        "description": description,
        "claim": claim,
        "observed": observed,
        "noul": noul,
        "latency_ms": round(latency * 1000),
        "screenshot": _screenshot_b64(page) if page is not None else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }


def run_local_app_flow_steps(page):
    """The ticket-admin app flow: login, role-gated verification, the
    real disambiguation tests (identical Edit buttons / identical
    Status dropdowns), create-record form, role switch back."""
    n = 0

    n += 1
    yield action_step(n, "find the username field", page,
                       "enter the username 'bryan'", "fill", "bryan")

    n += 1
    yield action_step(n, "find the password field", page,
                       "enter the password 'demo123'", "fill", "demo123")

    n += 1
    yield action_step(n, "find the login button", page,
                       "submit the login form", "click")

    n += 1
    delete_count = count_buttons_named_like(page, "Delete")
    yield verify_step(n, "Delete buttons hidden for default role",
                       "No Delete buttons should be visible for the current role.",
                       f"Number of visible Delete buttons: {delete_count}", page=page)

    n += 1
    yield action_step(n, "find the role selector (not a per-ticket status dropdown)",
                       page, "switch the current role to Merchant", "select", "Merchant")

    n += 1
    delete_count = count_buttons_named_like(page, "Delete")
    yield verify_step(n, "Delete buttons appear for Merchant role",
                       "Delete buttons should now be visible for the Merchant role.",
                       f"Number of visible Delete buttons: {delete_count}", page=page)

    n += 1
    yield action_step(
        n, "find the Edit button for Marcus Webb's ticket "
           "(3 identical 'Edit' buttons on the page -- the real disambiguation test)",
        page, "edit the ticket belonging to Marcus Webb", "click",
        ground_truth_fn=lambda p: p.locator("tr.row-highlight").get_attribute("data-customer"),
        ground_truth_expected="Marcus Webb",
    )

    n += 1
    yield action_step(
        n, "find the status dropdown for Alice Chen's ticket "
           "(3 identical 'Status' dropdowns -- same disambiguation test)",
        page, "set the status to 'Closed' for Alice Chen's ticket", "select", "Closed",
        ground_truth_fn=lambda p: p.locator(
            'tr[data-customer="Alice Chen"] .status-select').input_value(),
        ground_truth_expected="Closed",
    )

    n += 1
    yield action_step(n, "find the New Ticket button", page,
                       "open the form to create a new ticket", "click")

    n += 1
    yield action_step(n, "find the customer-name field (not the description textarea)",
                       page, "enter the customer name 'Devon Brooks' in the new ticket form",
                       "fill", "Devon Brooks")

    n += 1
    yield action_step(n, "find the Store dropdown in the new ticket form", page,
                       "set the store to 'Harbor Knits' for the new ticket",
                       "select", "Harbor Knits")

    n += 1
    yield action_step(n, "find the Urgent checkbox", page,
                       "mark the new ticket as urgent", "check")

    n += 1
    yield action_step(n, "find the Submit button (not Cancel)", page,
                       "submit the new ticket form", "click")

    n += 1
    new_row_text = page.locator('tr[data-customer="Devon Brooks"]').inner_text()
    yield verify_step(n, "new ticket created correctly",
                       "The new ticket should show customer 'Devon Brooks' "
                       "and store 'Harbor Knits'.",
                       f"New row contents: '{new_row_text}'", page=page)

    n += 1
    yield action_step(n, "find the role selector", page,
                       "switch the current role to Shopper", "select", "Shopper")

    n += 1
    delete_count = count_buttons_named_like(page, "Delete")
    yield verify_step(n, "Delete buttons hidden again for Shopper role",
                       "No Delete buttons should be visible for the current role.",
                       f"Number of visible Delete buttons: {delete_count}", page=page)


def run_saucedemo_flow_steps(page):
    """The original SauceDemo login/cart flow, restructured to yield
    the same structured step shape as the local app flow."""
    n = 0

    n += 1
    yield action_step(n, "find the username field", page,
                       "enter the username 'standard_user'", "fill", "standard_user")

    n += 1
    yield action_step(n, "find the password field", page,
                       "enter the password 'secret_sauce'", "fill", "secret_sauce")

    n += 1
    yield action_step(n, "find the login button", page,
                       "submit the login form", "click")
    page.wait_for_load_state("networkidle")

    n += 1
    yield action_step(n, "find the add-to-cart button for Sauce Labs Bike Light",
                       page, "add the 'Sauce Labs Bike Light' product to the cart", "click")

    n += 1
    badge = page.locator(".shopping_cart_badge")
    cart_text = badge.inner_text() if badge.count() else "(no badge found)"
    yield verify_step(n, "cart badge check",
                       "The cart badge should show exactly 1 item.",
                       f"Cart badge text: '{cart_text}'", page=page)

    n += 1
    yield action_step(
        n, "find the cart icon/link", page, "open the shopping cart", "click",
        ground_truth_fn=lambda p: p.locator(".inventory_item_name").all_inner_texts(),
        ground_truth_expected=["Sauce Labs Bike Light"],
    )


FLOWS = {
    "local_app": {
        "label": "Local ticket-admin app (disambiguation test)",
        "url": "/test-app",
        "runner": run_local_app_flow_steps,
    },
    "saucedemo": {
        "label": "SauceDemo (public demo site)",
        "url": "https://www.saucedemo.com/",
        "runner": run_saucedemo_flow_steps,
    },
}
