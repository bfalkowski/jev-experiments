"""Record decision points for the ambiguity study.

Study question: when a browser test agent is asked to act on an element,
does it know when it cannot tell which element is meant, or when the
element is not on the page at all?

For every decision point this saves the candidate list at several levels
of surrounding context, so the same page can be shown to an engine with
more or less information:

  parent   text of the element's immediate container (what many tools show)
  grand    two containers up, first 120 characters (the default used so far)
  wide     three containers up, first 250 characters

A point is one of three kinds:
  present   the goal's element is on the page (labels = correct indexes)
  missing   the goal refers to something that is not on the page
            (a role-gated button that is hidden, a row that does not exist,
            a product that is not sold); the right response is to abstain

`key` is the text that tells the target apart from look-alikes (a
customer name, a product name, a price). Whether a point is answerable
under a given context level is computed later from the candidate text,
not decided by hand.

Usage:
  python record_ambiguity.py --local-url http://127.0.0.1:8765/local_test_app.html \\
                             --sauce-url http://127.0.0.1:4173/
Writes data/ambiguity.jsonl and data/screens_ambiguity/*.jpg.
"""

import argparse
import json
import os
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from browser_actions import get_interactive_elements
from record_decisions import label_index, local_login

OUT = "data/ambiguity.jsonl"
SCREENS = "data/screens_ambiguity"

CONTEXT_JS = """
([el, levels, maxLen]) => {
    let node = el;
    for (let i = 0; i < levels && node.parentElement; i++) node = node.parentElement;
    return (node.innerText || '').split(/\\s+/).join(' ').trim().slice(0, maxLen);
}
"""
LEVELS = {"parent": (1, 120), "grand": (2, 120), "wide": (3, 250)}


def candidates_with_contexts(page):
    elements = get_interactive_elements(page)
    for e in elements:
        handle = page.get_by_role(e["role"]).nth(e["nth"]).element_handle()
        e["contexts"] = {}
        for name, (lvl, n) in LEVELS.items():
            try:
                e["contexts"][name] = page.evaluate(CONTEXT_JS, [handle, lvl, n])
            except Exception:  # noqa: BLE001
                e["contexts"][name] = ""
    return elements


class Rec:
    def __init__(self, page):
        self.page = page
        self.points = []

    def add(self, pid, site, kind, goal, key=None, target=None, alts=(), note=""):
        cands = candidates_with_contexts(self.page)
        labels = []
        if kind == "present":
            for sel in (target, *alts):
                i = label_index(self.page, cands, sel)
                if i is not None and i not in labels:
                    labels.append(i)
            if not labels:
                raise RuntimeError(f"{pid}: target not in candidate list")
        elif target is not None:
            # A 'missing' point must really be missing.
            assert self.page.locator(target).count() == 0, f"{pid}: target unexpectedly present"
        shot = os.path.join(SCREENS, f"{pid}.jpg")
        self.page.screenshot(path=shot, type="jpeg", quality=55, full_page=True)
        self.points.append({"id": pid, "site": site, "kind": kind, "goal": goal, "key": key,
                            "labels": labels, "candidates": cands, "note": note,
                            "url": self.page.url, "screenshot": shot})
        print(f"  {pid:<40} {kind:<8} labels={labels} n={len(cands)}")


def local(rec, url):
    page = rec.page
    # --- 3 rows, Shopper role (no Delete buttons)
    local_login(page, url)
    for c in ("Alice Chen", "Marcus Webb", "Priya Nair"):
        slug = c.split()[0].lower()
        rec.add(f"L3-edit-{slug}", "local", "present", f"edit the ticket belonging to {c}", c,
                f'tr[data-customer="{c}"] .edit-btn')
        rec.add(f"L3-status-{slug}", "local", "present", f"set the status to 'Closed' for {c}'s ticket", c,
                f'tr[data-customer="{c}"] .status-select')
    rec.add("L3-edit-by-store", "local", "present", "edit the ticket for the Atlas Stationery store",
            "Atlas Stationery", 'tr[data-customer="Marcus Webb"] .edit-btn', note="content-based goal")
    rec.add("L3-status-by-id", "local", "present", "change the status of ticket 103", "103",
            'tr[data-customer="Priya Nair"] .status-select', note="content-based goal")
    rec.add("L3-role", "local", "present", "switch the current role to Merchant", None, "#role-select")
    rec.add("L3-miss-delete-hidden", "local", "missing", "delete the ticket belonging to Alice Chen",
            "Alice Chen", ".delete-btn", note="Delete is hidden for the Shopper role")
    rec.add("L3-miss-edit-nobody", "local", "missing", "edit the ticket belonging to Devon Brooks",
            "Devon Brooks", 'tr[data-customer="Devon Brooks"]', note="no such row")
    rec.add("L3-miss-status-offpage", "local", "missing", "set the status to 'Closed' for Omar Haddad's ticket",
            "Omar Haddad", 'tr[data-customer="Omar Haddad"]', note="row exists only with more rows")
    rec.add("L3-miss-export", "local", "missing", "export the tickets to a CSV file", None, None,
            note="no export feature")
    # --- 3 rows, Merchant role
    page.select_option("#role-select", "Merchant")
    for c in ("Alice Chen", "Priya Nair"):
        slug = c.split()[0].lower()
        rec.add(f"L3-delete-{slug}", "local", "present", f"delete the ticket belonging to {c}", c,
                f'tr[data-customer="{c}"] .delete-btn')
    rec.add("L3-delete-by-store", "local", "present", "delete the ticket from the Harbor Knits store",
            "Harbor Knits", 'tr[data-customer="Priya Nair"] .delete-btn', note="content-based goal")

    # --- 10 and 25 rows
    for rows, custs, absent in ((10, ("Marcus Webb", "Ben Carter"), "Maya Cohen"),
                                (25, ("Iris Nakamura", "Maya Cohen"), "Devon Brooks")):
        local_login(page, f"{url}?rows={rows}")
        for c in custs:
            slug = c.split()[0].lower()
            rec.add(f"L{rows}-edit-{slug}", "local", "present", f"edit the ticket belonging to {c}", c,
                    f'tr[data-customer="{c}"] .edit-btn')
            rec.add(f"L{rows}-status-{slug}", "local", "present",
                    f"set the status to 'Closed' for {c}'s ticket", c,
                    f'tr[data-customer="{c}"] .status-select')
        rec.add(f"L{rows}-miss-edit-{absent.split()[0].lower()}", "local", "missing",
                f"edit the ticket belonging to {absent}", absent, f'tr[data-customer="{absent}"]',
                note=f"not among the {rows} rows")


PRODUCTS = {
    "Sauce Labs Backpack": "sauce-labs-backpack",
    "Sauce Labs Bike Light": "sauce-labs-bike-light",
    "Sauce Labs Bolt T-Shirt": "sauce-labs-bolt-t-shirt",
    "Sauce Labs Fleece Jacket": "sauce-labs-fleece-jacket",
    "Sauce Labs Onesie": "sauce-labs-onesie",
    "Test.allTheThings() T-Shirt (Red)": "test.allthethings()-t-shirt-(red)",
}


def sauce(rec, url):
    page = rec.page
    page.goto(url)
    page.evaluate("() => localStorage.clear()")
    page.goto(url)
    page.fill('[data-test="username"]', "standard_user")
    page.fill('[data-test="password"]', "secret_sauce")
    page.click('[data-test="login-button"]')
    page.wait_for_selector(".inventory_list")

    for name in ("Sauce Labs Backpack", "Sauce Labs Bolt T-Shirt", "Sauce Labs Onesie"):
        rec.add(f"S-add-{PRODUCTS[name]}", "saucedemo", "present", f"add the '{name}' to the cart", name,
                f'[data-test="add-to-cart-{PRODUCTS[name]}"]')
    rec.add("S-add-by-price", "saucedemo", "present", "add the $7.99 item to the cart", "$7.99",
            '[data-test="add-to-cart-sauce-labs-onesie"]', note="content-based goal (price)")
    rec.add("S-add-red", "saucedemo", "present", "add the red T-shirt to the cart", "(Red)",
            f'[data-test="add-to-cart-{PRODUCTS["Test.allTheThings() T-Shirt (Red)"]}"]',
            note="content-based goal")
    rec.add("S-miss-water-bottle", "saucedemo", "missing", "add the 'Sauce Labs Water Bottle' to the cart",
            "Sauce Labs Water Bottle", None, note="product not sold")
    rec.add("S-miss-remove-not-in-cart", "saucedemo", "missing",
            "remove the 'Sauce Labs Fleece Jacket' from the cart", "Sauce Labs Fleece Jacket",
            '[data-test="remove-sauce-labs-fleece-jacket"]', note="not in cart, so no Remove button")

    for name in ("Sauce Labs Backpack", "Sauce Labs Bike Light", "Sauce Labs Onesie"):
        page.click(f'[data-test="add-to-cart-{PRODUCTS[name]}"]')
    rec.add("S-inv-remove-bike-light", "saucedemo", "present", "remove the 'Sauce Labs Bike Light' from the cart",
            "Sauce Labs Bike Light", '[data-test="remove-sauce-labs-bike-light"]')
    rec.add("S-inv-add-fleece", "saucedemo", "present", "add the 'Sauce Labs Fleece Jacket' to the cart",
            "Sauce Labs Fleece Jacket", '[data-test="add-to-cart-sauce-labs-fleece-jacket"]')

    page.click('[data-test="shopping-cart-link"]')
    page.wait_for_selector(".cart_list")
    for name in ("Sauce Labs Backpack", "Sauce Labs Onesie"):
        rec.add(f"S-cart-remove-{PRODUCTS[name]}", "saucedemo", "present",
                f"remove the '{name}' from the cart", name, f'[data-test="remove-{PRODUCTS[name]}"]')
    rec.add("S-cart-miss-bolt", "saucedemo", "missing", "remove the 'Sauce Labs Bolt T-Shirt' from the cart",
            "Sauce Labs Bolt T-Shirt", '[data-test="remove-sauce-labs-bolt-t-shirt"]', note="not in cart")
    rec.add("S-cart-miss-promo", "saucedemo", "missing", "apply a promo code to the order", None, None,
            note="no promo code field")
    rec.add("S-cart-checkout", "saucedemo", "present", "proceed to checkout", None, '[data-test="checkout"]')

    page.click('[data-test="checkout"]')
    page.wait_for_selector('[data-test="firstName"]')
    rec.add("S-co-zip", "saucedemo", "present", "enter the postal code '06510'", None, '[data-test="postalCode"]')
    rec.add("S-co-miss-phone", "saucedemo", "missing", "enter the phone number for the order", None, None,
            note="no phone field")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-url", required=True)
    ap.add_argument("--sauce-url", required=True)
    ap.add_argument("--sauce-version", default="")
    args = ap.parse_args()
    os.makedirs(SCREENS, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        rec = Rec(page)
        print("local app")
        local(rec, args.local_url)
        print("saucedemo")
        sauce(rec, args.sauce_url)
        browser.close()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUT, "w") as f:
        for pt in rec.points:
            pt["recorded_at"] = now
            pt["sauce_version"] = args.sauce_version if pt["site"] == "saucedemo" else None
            f.write(json.dumps(pt) + "\n")
    kinds = {}
    for pt in rec.points:
        kinds[pt["kind"]] = kinds.get(pt["kind"], 0) + 1
    print(f"\n{len(rec.points)} points: {kinds}")


if __name__ == "__main__":
    main()
