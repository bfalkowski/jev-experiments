"""Record frozen decision points for the element-selection experiment.

Walks the local ticket-admin app (3, 10 and 25 rows) and a self-hosted
build of SauceDemo with deterministic selectors, and at each decision
point saves:

  - the goal text plus two hand-written paraphrases
  - the full candidate list exactly as get_interactive_elements() builds it
    (role, nth, name, context) -- the same list every engine will see
  - the index of the correct candidate, found by DOM identity (the
    candidate's element handle IS the target element), or null when the
    target never made it into the candidate list (an enumeration miss)
  - a screenshot and some metadata

Usage:
  python record_decisions.py --local-url http://127.0.0.1:8765/local_test_app.html \\
                             --sauce-url http://127.0.0.1:4173/
Writes data/decisions.jsonl and data/screens/*.png.
"""

import argparse
import json
import os
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from browser_actions import get_interactive_elements

OUT_DIR = "data"
SCREEN_DIR = os.path.join(OUT_DIR, "screens")


def label_index(page, elements, target_selector):
    """Return the index of the candidate whose DOM node is the target, or
    None if the target is not in the candidate list."""
    target = page.locator(target_selector)
    if target.count() != 1:
        raise RuntimeError(f"target selector matched {target.count()} nodes: {target_selector}")
    handle = target.element_handle()
    for i, e in enumerate(elements):
        cand = page.get_by_role(e["role"]).nth(e["nth"]).element_handle()
        if cand is not None and page.evaluate("([a, b]) => a === b", [cand, handle]):
            return i
    return None


class Recorder:
    def __init__(self, page):
        self.page = page
        self.points = []

    def record(self, pid, site, case_type, goals, target_selector, action, value=None, meta=None,
               alt_selectors=()):
        """alt_selectors: other elements that do exactly the same thing (e.g. a
        product image and its title both open the product page). Picking any
        of them counts as correct."""
        elements = get_interactive_elements(self.page)
        idx = label_index(self.page, elements, target_selector)
        labels = [idx] if idx is not None else []
        for alt in alt_selectors:
            j = label_index(self.page, elements, alt)
            if j is not None and j not in labels:
                labels.append(j)
        shot = os.path.join(SCREEN_DIR, f"{pid}.jpg")
        self.page.screenshot(path=shot, type="jpeg", quality=55, full_page=True)
        self.points.append({
            "id": pid,
            "site": site,
            "kind": "pick",
            "case_type": case_type,
            "goals": goals,
            "action": action,
            "value": value,
            "candidates": elements,
            "label": idx,
            "labels": labels,
            "target_selector": target_selector,
            "alt_selectors": list(alt_selectors),
            "url": self.page.url,
            "screenshot": shot,
            "meta": meta or {},
        })
        status = "MISS (not in candidate list)" if idx is None else f"label={idx}/{len(elements)}"
        print(f"  {pid:<32} {status}")


    def verify(self, pid, site, claim, observed, label):
        """A yes/no verification point: does the observed page evidence
        support the claim? The observation is read from the live page; the
        label is set by construction (claims are written to be true or false
        for that observation)."""
        self.points.append({
            "id": pid, "site": site, "kind": "verify", "case_type": "verify",
            "claim": claim, "observed": observed, "label": bool(label), "labels": [bool(label)],
            "url": self.page.url,
        })
        print(f"  {pid:<32} verify label={label}  observed={observed[:60]!r}")


# ---------------------------------------------------------------- local app

EDIT_GOALS = [
    "edit the ticket belonging to {c}",
    "open {c}'s ticket for editing",
    "I need to change the support ticket from {c}",
]
STATUS_GOALS = [
    "set the status to 'Closed' for {c}'s ticket",
    "mark {c}'s ticket as closed",
    "change the status on the row for {c}",
]
DELETE_GOALS = [
    "delete the ticket belonging to {c}",
    "remove {c}'s ticket",
    "get rid of the support ticket from {c}",
]
ROLE_GOALS = [
    "switch the current role to Merchant",
    "change which role I am acting as",
    "use the role picker at the top to become a Merchant",
]


def local_login(page, url):
    page.goto(url)
    page.fill("#username", "bryan")
    page.fill("#password", "demo123")
    page.click("#login-btn")


def record_local(rec, base_url):
    page = rec.page
    # Login screen (only once; it does not depend on the row count).
    page.goto(base_url)
    rec.record("local-login-username", "local", "unique",
               ["enter the username 'bryan'", "type my user name", "fill in the login name field"],
               "#username", "fill", "bryan")
    rec.record("local-login-password", "local", "unique",
               ["enter the password 'demo123'", "type my password", "fill in the secret for signing in"],
               "#password", "fill", "demo123")
    rec.record("local-login-submit", "local", "unique",
               ["submit the login form", "sign in", "log me in"],
               "#login-btn", "click")

    picks = {3: ["Alice Chen", "Marcus Webb", "Priya Nair"],
             10: ["Marcus Webb", "Omar Haddad", "Ben Carter"],
             25: ["Priya Nair", "Iris Nakamura", "Maya Cohen"]}

    for rows, customers in picks.items():
        url = f"{base_url}?rows={rows}"
        local_login(page, url)
        n_rows = page.locator("#tickets-body tr").count()
        assert n_rows == rows, (rows, n_rows)
        case = f"repeated-{rows}"
        for c in customers:
            slug = c.lower().replace(" ", "-")
            rec.record(f"local-r{rows}-edit-{slug}", "local", case,
                       [g.format(c=c) for g in EDIT_GOALS],
                       f'tr[data-customer="{c}"] .edit-btn', "click", meta={"rows": rows})
            rec.record(f"local-r{rows}-status-{slug}", "local", case,
                       [g.format(c=c) for g in STATUS_GOALS],
                       f'tr[data-customer="{c}"] .status-select', "select", "Closed", meta={"rows": rows})
        rec.record(f"local-r{rows}-role-select", "local", f"lookalike-dropdown-{rows}",
                   ROLE_GOALS, "#role-select", "select", "Merchant", meta={"rows": rows})

        if rows == 3:
            n = page.locator(".delete-btn").count()
            obs = f"Number of visible Delete buttons: {n}"
            rec.verify("local-v-shopper-no-delete", "local",
                       "No Delete buttons should be visible for the current role.", obs, n == 0)
            rec.verify("local-v-shopper-has-delete", "local",
                       "Delete buttons should be visible for the current role.", obs, n > 0)
            page.select_option('tr[data-customer="Alice Chen"] .status-select', "Closed")
            val = page.locator('tr[data-customer="Alice Chen"] .status-select').input_value()
            rec.verify("local-v-alice-closed", "local", "Alice Chen's ticket should now be Closed.",
                       f"Row for Alice Chen: status dropdown value = '{val}'", val == "Closed")
            val2 = page.locator('tr[data-customer="Marcus Webb"] .status-select').input_value()
            rec.verify("local-v-marcus-closed", "local", "Marcus Webb's ticket should now be Closed.",
                       f"Row for Marcus Webb: status dropdown value = '{val2}'", val2 == "Closed")

        page.select_option("#role-select", "Merchant")
        if rows == 3:
            n = page.locator(".delete-btn").count()
            obs = f"Number of visible Delete buttons: {n}"
            rec.verify("local-v-merchant-no-delete", "local",
                       "No Delete buttons should be visible for the current role.", obs, n == 0)
            rec.verify("local-v-merchant-has-delete", "local",
                       "Delete buttons should now be visible for the Merchant role.", obs, n > 0)
        for c in customers[:2]:
            slug = c.lower().replace(" ", "-")
            rec.record(f"local-r{rows}-delete-{slug}", "local", case,
                       [g.format(c=c) for g in DELETE_GOALS],
                       f'tr[data-customer="{c}"] .delete-btn', "click", meta={"rows": rows})
        page.select_option("#role-select", "Shopper")

        if rows in (3, 25):
            if rows == 3:
                rec.record("local-r3-new-ticket", "local", "unique",
                           ["open the form to create a new ticket", "start a new ticket", "I want to file another ticket"],
                           "#new-ticket-btn", "click", meta={"rows": rows})
            page.click("#new-ticket-btn")
            rec.record(f"local-r{rows}-form-store", "local", f"lookalike-dropdown-{rows}",
                       ["set the store to 'Harbor Knits' for the new ticket",
                        "choose which store the new ticket is for",
                        "pick Harbor Knits in the new ticket form"],
                       "#nt-store", "select", "Harbor Knits", meta={"rows": rows})
            if rows == 3:
                rec.record("local-r3-form-customer", "local", "form-field",
                           ["enter the customer name 'Devon Brooks' in the new ticket form",
                            "type the new customer's name",
                            "fill in who the new ticket is for"],
                           "#nt-customer", "fill", "Devon Brooks")
                rec.record("local-r3-form-priority", "local", "lookalike-dropdown-3",
                           ["set the priority of the new ticket to High",
                            "make the new ticket high priority",
                            "change how important the new ticket is"],
                           "#nt-priority", "select", "High")
                rec.record("local-r3-form-urgent", "local", "form-field",
                           ["mark the new ticket as urgent", "flag the new ticket urgent", "tick the urgent box"],
                           "#nt-urgent", "check")
                rec.record("local-r3-form-description", "local", "form-field",
                           ["describe the problem in the new ticket", "write the ticket description",
                            "add details about the issue to the new ticket"],
                           "#nt-description", "fill", "Order arrived damaged")
                rec.record("local-r3-form-submit", "local", "near-duplicate-button",
                           ["submit the new ticket form", "save the new ticket", "create the ticket"],
                           "#nt-submit", "click")
                rec.record("local-r3-form-cancel", "local", "near-duplicate-button",
                           ["cancel the new ticket form", "close the new ticket form without saving",
                            "never mind, discard the new ticket"],
                           "#nt-cancel", "click")
                page.fill("#nt-customer", "Devon Brooks")
                page.select_option("#nt-store", "Harbor Knits")
                page.click("#nt-submit")
                row = " ".join(page.locator('tr[data-customer="Devon Brooks"]').inner_text().split())
                obs = f"New row contents: '{row}'"
                rec.verify("local-v-new-row-ok", "local",
                           "The new ticket should show customer 'Devon Brooks' and store 'Harbor Knits'.",
                           obs, "Devon Brooks" in row and "Harbor Knits" in row)
                rec.verify("local-v-new-row-wrong-store", "local",
                           "The new ticket should show customer 'Devon Brooks' and store 'Atlas Stationery'.",
                           obs, "Atlas Stationery" in row)


# ---------------------------------------------------------------- SauceDemo

PRODUCTS = [
    ("Sauce Labs Backpack", "sauce-labs-backpack", 4),
    ("Sauce Labs Bike Light", "sauce-labs-bike-light", 0),
    ("Sauce Labs Bolt T-Shirt", "sauce-labs-bolt-t-shirt", 1),
    ("Sauce Labs Fleece Jacket", "sauce-labs-fleece-jacket", 5),
    ("Sauce Labs Onesie", "sauce-labs-onesie", 2),
    ("Test.allTheThings() T-Shirt (Red)", "test.allthethings()-t-shirt-(red)", 3),
]


def record_sauce(rec, base_url):
    page = rec.page
    page.goto(base_url)
    page.evaluate("() => localStorage.clear()")
    page.goto(base_url)
    rec.record("sauce-login-username", "saucedemo", "unique",
               ["enter the username 'standard_user'", "type my user name", "fill in the login name"],
               '[data-test="username"]', "fill", "standard_user")
    rec.record("sauce-login-password", "saucedemo", "unique",
               ["enter the password 'secret_sauce'", "type my password", "fill in the secret for signing in"],
               '[data-test="password"]', "fill", "secret_sauce")
    rec.record("sauce-login-submit", "saucedemo", "unique",
               ["submit the login form", "sign in", "log me in"],
               '[data-test="login-button"]', "click")

    page.fill('[data-test="username"]', "standard_user")
    page.fill('[data-test="password"]', "secret_sauce")
    page.click('[data-test="login-button"]')
    page.wait_for_selector(".inventory_list")

    for name, slug, _ in PRODUCTS:
        rec.record(f"sauce-add-{slug}", "saucedemo", "repeated-6",
                   [f"add the '{name}' product to the cart",
                    f"put the {name} in my cart",
                    f"I want to buy the {name}"],
                   f'[data-test="add-to-cart-{slug}"]', "click")
    for name, _, item_id in PRODUCTS[2:4]:
        rec.record(f"sauce-open-{item_id}", "saucedemo", "repeated-6",
                   [f"open the product page for '{name}'",
                    f"see the details of the {name}",
                    f"click through to the {name} listing"],
                   f'[data-test="item-{item_id}-title-link"]', "click",
                   alt_selectors=[f'[data-test="item-{item_id}-img-link"]'])
    rec.record("sauce-sort", "saucedemo", "unique",
               ["sort the products by price, low to high", "change the product sort order",
                "order the list so the cheapest items come first"],
               '[data-test="product-sort-container"]', "select", "lohi")

    # Add three items, then the page has a mix of Add and Remove buttons.
    for _, slug, _ in (PRODUCTS[0], PRODUCTS[1], PRODUCTS[4]):
        page.click(f'[data-test="add-to-cart-{slug}"]')
    for name, slug, _ in (PRODUCTS[1], PRODUCTS[4]):
        rec.record(f"sauce-inv-remove-{slug}", "saucedemo", "repeated-mixed",
                   [f"remove the '{name}' from the cart",
                    f"take the {name} back out of my cart",
                    f"I changed my mind about the {name}"],
                   f'[data-test="remove-{slug}"]', "click")
    badge = page.locator(".shopping_cart_badge").inner_text()
    obs = f"Cart badge text: '{badge}'"
    rec.verify("sauce-v-badge-one", "saucedemo", "The cart badge should show exactly 1 item.", obs, badge == "1")
    rec.verify("sauce-v-badge-three", "saucedemo", "The cart badge should show 3 items.", obs, badge == "3")
    rec.record("sauce-cart-link", "saucedemo", "icon-only",
               ["open the shopping cart", "go to my cart", "view what is in the cart"],
               '[data-test="shopping-cart-link"]', "click")

    page.click('[data-test="shopping-cart-link"]')
    page.wait_for_selector(".cart_list")
    for name, slug, _ in (PRODUCTS[0], PRODUCTS[4]):
        rec.record(f"sauce-cart-remove-{slug}", "saucedemo", "repeated-3",
                   [f"remove the '{name}' from the cart",
                    f"delete the {name} from my order",
                    f"I no longer want the {name}"],
                   f'[data-test="remove-{slug}"]', "click")
    rec.record("sauce-cart-checkout", "saucedemo", "near-duplicate-button",
               ["proceed to checkout", "check out", "go on to pay for these items"],
               '[data-test="checkout"]', "click")
    rec.record("sauce-cart-continue-shopping", "saucedemo", "near-duplicate-button",
               ["continue shopping", "go back to the product list", "keep browsing for more items"],
               '[data-test="continue-shopping"]', "click")

    names = page.locator(".inventory_item_name").all_inner_texts()
    obs = f"Items listed in the cart: {names}"
    rec.verify("sauce-v-cart-has-backpack", "saucedemo", "The cart should contain the Sauce Labs Backpack.",
               obs, "Sauce Labs Backpack" in names)
    rec.verify("sauce-v-cart-has-jacket", "saucedemo", "The cart should contain the Sauce Labs Fleece Jacket.",
               obs, "Sauce Labs Fleece Jacket" in names)

    page.click('[data-test="checkout"]')
    page.wait_for_selector('[data-test="firstName"]')
    rec.record("sauce-checkout-first", "saucedemo", "form-field",
               ["enter the first name 'Bryan'", "type my first name", "fill in the given name for the order"],
               '[data-test="firstName"]', "fill", "Bryan")
    rec.record("sauce-checkout-last", "saucedemo", "form-field",
               ["enter the last name 'Falkowski'", "type my surname", "fill in the family name for the order"],
               '[data-test="lastName"]', "fill", "Falkowski")
    rec.record("sauce-checkout-zip", "saucedemo", "form-field",
               ["enter the postal code '06510'", "type my zip code", "fill in where to ship, by postal code"],
               '[data-test="postalCode"]', "fill", "06510")
    rec.record("sauce-checkout-continue", "saucedemo", "near-duplicate-button",
               ["continue to the next checkout step", "go on to the order overview", "proceed with checkout"],
               '[data-test="continue"]', "click")
    rec.record("sauce-checkout-cancel", "saucedemo", "near-duplicate-button",
               ["cancel checkout", "abandon this checkout", "back out of the checkout form"],
               '[data-test="cancel"]', "click")

    page.click('[data-test="continue"]')
    err = page.locator('[data-test="error"]')
    msg = err.inner_text() if err.count() else "(no error shown)"
    obs = f"After clicking Continue with empty fields, the page shows: '{msg}'"
    rec.verify("sauce-v-form-accepted", "saucedemo", "The checkout form should accept the order details and move on.",
               obs, "error" not in msg.lower())
    rec.verify("sauce-v-form-error-first", "saucedemo",
               "The form should show an error because the first name is missing.", obs,
               "first name is required" in msg.lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-url", required=True)
    ap.add_argument("--sauce-url", required=True)
    ap.add_argument("--sauce-version", default="")
    args = ap.parse_args()

    os.makedirs(SCREEN_DIR, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        rec = Recorder(page)
        print("local app")
        record_local(rec, args.local_url)
        print("saucedemo")
        record_sauce(rec, args.sauce_url)
        browser.close()

    recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(os.path.join(OUT_DIR, "decisions.jsonl"), "w") as f:
        for pt in rec.points:
            pt["recorded_at"] = recorded_at
            pt.setdefault("meta", {}).setdefault("sauce_version", args.sauce_version if pt["site"] == "saucedemo" else None)
            f.write(json.dumps(pt) + "\n")
    misses = sum(1 for pt in rec.points if pt.get("kind") == "pick" and pt["label"] is None)
    print(f"\n{len(rec.points)} decision points, {misses} enumeration misses")


if __name__ == "__main__":
    main()
