# jev-experiments

A weekend project comparing two ways of picking the right element on a
generated web page: [TypeSafe AI's Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
(a purpose-built "System One" structured-decision model) against a
general-purpose Claude tool-use loop driving the real
[`microsoft/playwright-mcp`](https://github.com/microsoft/playwright-mcp)
server.

The motivating problem: evaluating AI-generated UIs (e.g. screens of
business apps built by a coding agent) via accessibility-tree browser
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

## Experiment: element selection (paper)

A controlled version of the comparison above, written up as a paper. The
question: how much of the gap between Jev and a general LLM comes from the
model, and how much from the interface around it (a full tool-use agent loop
versus one constrained question)?

**Conditions**

| ID | Engine | What it sees | How it answers |
|---|---|---|---|
| J | Jev (`jev-latest`) | Goal + numbered candidate list with row context | `choice` question (index + confidence); `noul` for yes/no checks |
| J-nc | Jev | Candidate names only, no context | same |
| C-S | Constrained Claude, `claude-haiku-4-5` | Same candidate list as J | One Messages call, forced tool whose only argument is an index limited to valid positions; forced boolean tool for yes/no checks |
| C-S-nc | Constrained Claude, `claude-haiku-4-5` | Names only | same |
| C-L | Constrained Claude, `claude-opus-5-5` | Same candidate list as J | same |
| A | Claude + playwright-mcp agent loop (`AGENT_MODEL`) | ~30 MCP tool definitions + page snapshots | Any tool call, live browser |

"Constrained" means Claude gets the same question Jev gets and can only answer
by picking from the list. `engines.candidate_line()` builds the candidate text
for both, so the input is identical.

**Dataset.** `record_decisions.py` walks the local ticket app (3, 10 and 25
identical rows) and a self-hosted build of SauceDemo
(`saucelabs/sample-app-web`) with known selectors and freezes every decision
point to `data/decisions.jsonl`: goal plus two paraphrases, the full candidate
list, the correct index (found by DOM identity, not by a model), and a
screenshot. It also records yes/no verification points whose observations are
read from the page.

**Running it**

```
# 1. record (needs the two apps served locally)
python3 -m http.server 8765 --bind 127.0.0.1            # serves local_test_app.html
npx vite preview --port 4173 --host 127.0.0.1           # in a sample-app-web checkout, after npm ci && npx vite build
uv run python record_decisions.py --local-url http://127.0.0.1:8765/local_test_app.html --sauce-url http://127.0.0.1:4173/

# 2. offline conditions (always dry-run first)
uv run python run_offline.py --dry-run
uv run python run_offline.py --stage pilot --max-usd 0.50
uv run python run_offline.py --conditions J,J-nc,C-S,C-S-nc --max-usd 3
uv run python run_offline.py --conditions C-L --max-usd 6

# 3. agent loop baseline
AGENT_MODEL=claude-opus-5-5 uv run python run_agent.py --flows local_app,saucedemo --runs 2 --max-usd 6

# 4. analysis
uv run --with matplotlib python analyze.py
```

Every runner refuses to spend without `--max-usd`, writes one JSONL line per
call, and resumes without repeating finished calls.

## Study 2: Can a browser test agent tell when it can't tell?

Study 1 (above) showed every engine near 100% once the candidate list carries
row context, so accuracy alone says little. Study 2 asks whether the engines
know when they *cannot* pick: the element is missing from the page, or several
look-alikes fit and the text shown does not separate them.

- `record_ambiguity.py` records 42 decision points (31 with the target present,
  11 where it is missing: a role-gated button that is hidden, a row that does
  not exist, a product that is not sold) and saves each candidate's surrounding
  text at three levels: `parent`, `grand` (the Study 1 default) and `wide`.
- `ambiguity_common.py` builds five views of each page (`none`,
  `none-shuffled`, `parent`, `grand`, `wide`) and decides, from the text alone,
  whether the target is identifiable in that view. Missing targets never are.
- Every engine (Jev, Haiku 4.5, Opus 5.5) can answer "none" (-1) and reports a
  confidence (Jev natively, Claude in its structured answer).
- `run_ambiguity.py` runs engines x points x views x repeats with the same
  budget cap, dry run and resume as Study 1.
- `analyze_ambiguity.py` scores each response as correct, wrong, over-abstain,
  abstain, lucky guess, wrong guess, or false action, and reports abstention
  on unanswerable points, silent guessing, first-match bias, order leakage
  (`none` vs `none-shuffled`) and whether confidence separates right from wrong.

```
uv run python run_ambiguity.py --dry-run
uv run python run_ambiguity.py --pilot --max-usd 0.30
uv run python run_ambiguity.py --engines jev,haiku --max-usd 1.50
uv run python run_ambiguity.py --engines opus --max-usd 3
uv run --with matplotlib python analyze_ambiguity.py
```

## Study 3: live runs on a local app

`orders_app.html` is a small order-management page served from the repo
(no backend, deterministic data, so tests cannot break). It has 25 orders
across three pages, identical "Edit order" icon buttons and "Status"
dropdowns on every row, an unlabeled Delete icon, customer names that are
not exposed as links, and a Viewer role that hides all row actions. Every
state change is logged to `window.__events`.

`run_live.py` runs 15 tasks (present, missing, off-page and scraper-blind)
against it in a real browser. Every engine starts from the same URL and
page state:

- **jev, haiku, opus**: our Playwright code scrapes the page with the same
  `get_interactive_elements()` and context used offline, the engine picks
  an element or "none", and our code performs the action.
- **agent**: Claude (`AGENT_MODEL`, default `claude-opus-5-5`) drives the
  browser itself through playwright-mcp and may reply `CANNOT_FIND`.

Outcomes are scored from the page's event log (what actually changed), not
from what the engine said.

```
uv run python run_live.py --dry-run
uv run python run_live.py --engines jev,haiku,opus --repeats 2 --max-usd 1
AGENT_MODEL=claude-opus-5-5 uv run python run_live.py --engines agent --repeats 2 --max-usd 6
```

## Secrets

`.env` is gitignored. A pre-commit hook (`gitleaks`) plus a GitHub Actions
CI backstop are the intended second and third layers of defense against
ever committing a key, but **as of this commit those aren't wired up
yet** -- add `.pre-commit-config.yaml` and `.github/workflows/gitleaks.yml`
before treating this repo as safe-by-default; until then, every commit
here has been reviewed by hand for key-shaped strings before pushing.
