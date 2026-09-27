"""
Prototype: Jev picking which page element to interact with, instead of
a chat LLM reading the full accessibility tree and deciding in free
text. Playwright still does all navigation and DOM interaction --
only the "which element" (and "does this state look right") decisions
move to Jev's Choice/Noul primitives.

Two targets:
  - SauceDemo (default): the public demo jev-browser's own README uses.
  - local_test_app.html, served locally: a small data-grid-style app
    (login, role switch gating which buttons render, a ticket table
    with identically-labeled Edit/Delete buttons per row, a create
    form with multiple dropdowns) built specifically to stress-test
    disambiguation and role-based verification, the same shape as
    evaluating a generated business app's screens.

Serve the local app first:
    python3 -m http.server 8000
    (from the same directory as local_test_app.html)

Then run:
    uv run python jev_browser_action.py http://localhost:8000/local_test_app.html
    uv run python jev_browser_action.py                      # SauceDemo
"""

import sys
from playwright.sync_api import sync_playwright
from typesafe_client import ask_typesafe

DEFAULT_URL = "https://www.saucedemo.com/"
INTERACTIVE_ROLES = ["textbox", "button", "checkbox", "link", "combobox"]


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
    """Covers the common real-world pattern this API's own aria-label
    fallback misses: an <input>/<select> with no aria-label, identified
    only by a wrapping <label> or a <label for="id">. Checkboxes almost
    always work this way -- an input has no innerText of its own, so
    without this, labeled checkboxes silently get no name at all."""
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
    since names collide constantly on real pages (grids, repeated
    row actions). Name + context snippet are just what gets shown to
    Jev to help it choose."""
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
    """Ask Jev which candidate element matches the goal, given name AND
    context for each. Returns (element_dict, confidence, elapsed_seconds)."""
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
    return elements[idx], answer["confidence"], elapsed


def act(page, element, action, value=None):
    """Act by exact (role, nth) position -- never by name, since names
    aren't unique identity."""
    locator = page.get_by_role(element["role"]).nth(element["nth"])
    if action == "fill":
        locator.fill(value)
    elif action == "click":
        locator.click()
    elif action == "check":
        locator.check()
    elif action == "select":
        locator.select_option(value)


def log_step(step_num, description, element, confidence, elapsed):
    print(f"\nStep {step_num}: {description}")
    print(f"  Jev picked: {element['role']} \"{element['name']}\" "
          f"(context: \"{element['context']}\")")
    print(f"  confidence {confidence:.2f}, {elapsed*1000:.0f} ms")


def jev_verify(claim, observed):
    """A Noul verification check, separate from element-picking."""
    state = {"claim": claim, "observed": observed}
    questions = {"verdict": {
        "type": "noul",
        "instructions": "Does the observed evidence support the claim?",
    }}
    response, elapsed = ask_typesafe(state, questions)
    return response["answers"]["verdict"]["noul"], elapsed


def count_buttons_named_like(page, substring):
    return len([e for e in get_interactive_elements(page)
                if e["role"] == "button" and substring.lower() in e["name"].lower()])


def run_saucedemo_flow(page):
    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("enter the username 'standard_user'", elements)
    log_step(1, "find the username field", el, conf, elapsed)
    act(page, el, "fill", "standard_user")

    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("enter the password 'secret_sauce'", elements)
    log_step(2, "find the password field", el, conf, elapsed)
    act(page, el, "fill", "secret_sauce")

    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("submit the login form", elements)
    log_step(3, "find the login button", el, conf, elapsed)
    act(page, el, "click")
    page.wait_for_load_state("networkidle")

    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element(
        "add the 'Sauce Labs Bike Light' product to the cart", elements
    )
    log_step(4, "find the add-to-cart button for Sauce Labs Bike Light", el, conf, elapsed)
    act(page, el, "click")

    badge = page.locator(".shopping_cart_badge")
    cart_text = badge.inner_text() if badge.count() else "(no badge found)"
    noul, elapsed, _usage = jev_verify("The cart badge should show exactly 1 item.",
                                f"Cart badge text: '{cart_text}'")
    print(f"\nStep 5 (verification): cart badge check")
    print(f"  observed: '{cart_text}'  ->  supports claim: {noul:.2f}  ({elapsed*1000:.0f} ms)")

    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("open the shopping cart", elements)
    log_step(6, "find the cart icon/link", el, conf, elapsed)
    act(page, el, "click")
    cart_items = page.locator(".inventory_item_name").all_inner_texts()
    print(f"\n  actual cart contents: {cart_items}")
    print(f"  correct pick? {'YES' if cart_items == ['Sauce Labs Bike Light'] else 'NO'}")


def run_local_app_flow(page):
    # --- Login ---
    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("enter the username 'bryan'", elements)
    log_step(1, "find the username field", el, conf, elapsed)
    act(page, el, "fill", "bryan")

    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("enter the password 'demo123'", elements)
    log_step(2, "find the password field", el, conf, elapsed)
    act(page, el, "fill", "demo123")

    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("submit the login form", elements)
    log_step(3, "find the login button", el, conf, elapsed)
    act(page, el, "click")

    # --- Baseline: Shopper role should see zero Delete buttons ---
    delete_count = count_buttons_named_like(page, "Delete")
    noul, elapsed, _usage = jev_verify(
        "No Delete buttons should be visible for the current role.",
        f"Number of visible Delete buttons: {delete_count}",
    )
    print(f"\nStep 4 (verification): Delete buttons hidden for default role")
    print(f"  observed count: {delete_count}  ->  supports claim: {noul:.2f}  ({elapsed*1000:.0f} ms)")

    # --- Switch role to Merchant (disambiguate the role selector from
    #     three per-row status selectors, now all comboboxes) ---
    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("switch the current role to Merchant", elements)
    log_step(5, "find the role selector (not a per-ticket status dropdown)", el, conf, elapsed)
    act(page, el, "select", "Merchant")

    delete_count = count_buttons_named_like(page, "Delete")
    noul, elapsed, _usage = jev_verify(
        "Delete buttons should now be visible for the Merchant role.",
        f"Number of visible Delete buttons: {delete_count}",
    )
    print(f"\nStep 6 (verification): Delete buttons appear for Merchant role")
    print(f"  observed count: {delete_count}  ->  supports claim: {noul:.2f}  ({elapsed*1000:.0f} ms)")

    # --- Real disambiguation test: three "Edit" buttons, identical name,
    #     only distinguishable via row context (customer name) ---
    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("edit the ticket belonging to Marcus Webb", elements)
    log_step(7, "find the Edit button for Marcus Webb's ticket (3 identical "
                 "'Edit' buttons on the page -- this is the real test)", el, conf, elapsed)
    act(page, el, "click")
    edited = page.locator("tr.row-highlight").get_attribute("data-customer")
    print(f"\n  actual row highlighted: {edited}")
    print(f"  correct pick? {'YES' if edited == 'Marcus Webb' else 'NO'}")

    # --- Same disambiguation problem, now with comboboxes: three status
    #     dropdowns, identical role, distinguished only by row context ---
    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element(
        "set the status to 'Closed' for Alice Chen's ticket", elements
    )
    log_step(8, "find the status dropdown for Alice Chen's ticket", el, conf, elapsed)
    act(page, el, "select", "Closed")
    alice_status = page.locator('tr[data-customer="Alice Chen"] .status-select').input_value()
    print(f"\n  actual status set: {alice_status}")
    print(f"  correct pick? {'YES' if alice_status == 'Closed' else 'NO'}")

    # --- Open the create-record form ---
    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("open the form to create a new ticket", elements)
    log_step(9, "find the New Ticket button", el, conf, elapsed)
    act(page, el, "click")

    # --- Fill the new-ticket form: textbox, two comboboxes, a checkbox ---
    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element(
        "enter the customer name 'Devon Brooks' in the new ticket form", elements
    )
    log_step(10, "find the customer-name field (not the description textarea)", el, conf, elapsed)
    act(page, el, "fill", "Devon Brooks")

    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element(
        "set the store to 'Harbor Knits' for the new ticket", elements
    )
    log_step(11, "find the Store dropdown in the new ticket form", el, conf, elapsed)
    act(page, el, "select", "Harbor Knits")

    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("mark the new ticket as urgent", elements)
    log_step(12, "find the Urgent checkbox", el, conf, elapsed)
    act(page, el, "check")

    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("submit the new ticket form", elements)
    log_step(13, "find the Submit button (not Cancel)", el, conf, elapsed)
    act(page, el, "click")

    new_row_text = page.locator('tr[data-customer="Devon Brooks"]').inner_text()
    noul, elapsed, _usage = jev_verify(
        "The new ticket should show customer 'Devon Brooks' and store 'Harbor Knits'.",
        f"New row contents: '{new_row_text}'",
    )
    print(f"\nStep 14 (verification): new ticket created correctly")
    print(f"  observed row: '{new_row_text}'  ->  supports claim: {noul:.2f}  ({elapsed*1000:.0f} ms)")

    # --- Switch back to Shopper, confirm Delete buttons disappear again ---
    elements = get_interactive_elements(page)
    el, conf, elapsed, _usage = jev_pick_element("switch the current role to Shopper", elements)
    log_step(15, "find the role selector", el, conf, elapsed)
    act(page, el, "select", "Shopper")

    delete_count = count_buttons_named_like(page, "Delete")
    noul, elapsed, _usage = jev_verify(
        "No Delete buttons should be visible for the current role.",
        f"Number of visible Delete buttons: {delete_count}",
    )
    print(f"\nStep 16 (verification): Delete buttons hidden again for Shopper role")
    print(f"  observed count: {delete_count}  ->  supports claim: {noul:.2f}  ({elapsed*1000:.0f} ms)")


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)

        if "saucedemo.com" in url:
            run_saucedemo_flow(page)
        elif "local_test_app" in url or "localhost" in url:
            run_local_app_flow(page)
        else:
            elements = get_interactive_elements(page)
            print(f"Discovered {len(elements)} interactive elements on {url}:\n")
            for i, e in enumerate(elements):
                print(f"  [{i}] {e['role']}: \"{e['name']}\"  context: \"{e['context']}\"")

        browser.close()


if __name__ == "__main__":
    main()
