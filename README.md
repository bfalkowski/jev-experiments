# jev-experiments

A weekend project comparing two ways of picking the right element on a
generated web page: [TypeSafe AI's Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
(a purpose-built "System One" structured-decision model) against a
general-purpose Claude tool-use loop driving the real
[`microsoft/playwright-mcp`](https://github.com/microsoft/playwright-mcp)
server.

The motivating problem: evaluating AI-generated UIs (e.g. a low-code
platform's dev-agent-generated app screens) via accessibility-tree browser
automation runs into pages with repeated, near-identical elements -- three
rows each with their own "Edit" button, three "Status" dropdowns with the same label.
Picking the *right* one requires using surrounding context (which row,
which section), not just the accessible name. This repo tests whether a
narrow, purpose-built decision model (Jev) actually does that better/
faster/cheaper than routing the same problem through a general LLM tool-use
agent -- with real, measured numbers instead of trusting either vendor's
blog post.

## What's here

- **`typesafe_client.py`** -- thin wrapper around the TypeSafe `systemone`
  API (`POST https://api.typesafe.ai/v1/systemone`).
- **`browser_actions.py`** / **`flows.py`** -- Playwright + Jev primitives
  and flow definitions (login, role-switching, disambiguating repeated
  elements, form filling, ground-truth checks) shared by the CLI script and
  the dashboard.
- **`mcp_playwright_agent.py`** -- the comparison engine: launches a real
  `npx @playwright/mcp@latest` server over stdio and drives it via a
  genuine Claude (Anthropic Messages API) tool-use loop, one atomic
  instruction at a time, at the same granularity as the Jev flow.
- **`dashboard.py`** / **`dashboard.html`** -- a local web dashboard
  (Flask + SSE) that runs either engine (or both, concurrently, in a
  split-screen view) against a flow and renders each step -- element
  picked, confidence, latency, cost, tokens, ground-truth pass/fail, and a
  screenshot of the page right after that step -- as it happens. Ends with
  a head-to-head comparison table when both engines are run together.
- **`local_test_app.html`** -- a self-contained mock ticket-admin app
  (login, role-gated Delete buttons, a tickets table with intentionally
  *identical* Edit/Status controls per row) built specifically to force
  real disambiguation, not accidentally-unique labels.
- **`jev_browser_action.py`** / **`jev_prototype.py`** -- earlier CLI-only
  prototypes, kept for reference; the dashboard is the actively maintained
  path.

## Setup

```
uv sync
uv run playwright install chromium
cp .env.example .env   # then fill in your real keys
```

`.env` needs:

- `TYPESAFE_API_KEY` -- required for the Jev engine.
- `ANTHROPIC_API_KEY` -- required for the Claude+playwright-mcp engine.

Node/npm must be available on the machine running the dashboard (the MCP
engine launches `npx @playwright/mcp@latest` as a subprocess on first use;
that first run downloads the package).

`.env` is gitignored and must never be committed. See **Secrets** below.

## Running it

```
uv run python dashboard.py
```

Open `http://127.0.0.1:8000/` (use the literal IPv4 address, not
`localhost` -- see the comment in `dashboard.py` for why that matters on
some machines). Pick a flow, pick an engine (Jev, Claude+playwright-mcp,
or both side by side), and hit Run. The "also open a real browser window"
checkbox is optional -- the embedded screenshot after each step is what
actually shows you the page state, on this page, without a popup.

Standalone CLI scripts also still work for quick checks:

```
uv run python jev_prototype.py        # basic Jev API smoke test
uv run python jev_browser_action.py   # CLI version of the browser flows
```

## What we found

On the local ticket-admin flow's two real disambiguation tests (picking
the correct row's Edit button and the correct row's Status dropdown out of
three identical-looking ones), both engines got it right. The difference
was cost and speed, not correctness:

|                    | Jev            | Claude + playwright-mcp |
|--------------------|----------------|--------------------------|
| Avg step latency   | ~440 ms        | ~4,900 ms                |
| Total run latency  | ~7 s           | ~88 s                    |
| Cost               | fractions of a cent (`$0.042`/M input tokens, output free) | ~$0.87 (Claude Sonnet 5 at `$2`/`$10` per M input/output tokens) |

Roughly an order of magnitude slower and more expensive for a general
tool-use loop to do the same narrow, atomic "which element is this"
decision that Jev is purpose-built for -- because every step re-sends the
full ~30-tool MCP schema plus a fresh page snapshot, regardless of how
trivial that step's action is. Numbers will vary by flow and by model;
re-run the dashboard's "both" mode to get current numbers for your own
case.

## Secrets

`.env` is gitignored. A pre-commit hook (`gitleaks`) plus a GitHub Actions
CI backstop are the intended second and third layers of defense against
ever committing a key, but **as of this commit those aren't wired up
yet** -- add `.pre-commit-config.yaml` and `.github/workflows/gitleaks.yml`
before treating this repo as safe-by-default; until then, every commit
here has been reviewed by hand for key-shaped strings before pushing.
