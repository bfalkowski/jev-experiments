"""Drives the SAME flow goals as flows.py, but through a genuine Claude
(Anthropic API) tool-use loop calling the REAL microsoft/playwright-mcp
server over stdio -- not a simulated stand-in. This is what most real
"playwright MCP" harnesses actually are: a general-purpose LLM in a
tool-use loop picking from generic browser_* tools, as opposed to Jev's
purpose-built structured-decision call per pick.

Primary sources checked before writing this (2026-09-19), because guessing
APIs from memory is how the earlier accessibility.snapshot() bug happened:
  - github.com/microsoft/playwright-mcp README -> `npx @playwright/mcp@latest`,
    stdio transport, tool list (browser_snapshot, browser_click, browser_type,
    browser_select_option, browser_evaluate, etc.)
  - github.com/modelcontextprotocol/python-sdk examples/clients/simple-chatbot
    -> the actual `from mcp import ClientSession, StdioServerParameters` /
    `from mcp.client.stdio import stdio_client` client pattern used below.
  - platform.claude.com tool-use docs -> the tool/tool_use/tool_result JSON
    shapes used below.
  - platform.claude.com models + pricing docs -> "claude-sonnet-5" is the
    current Sonnet-tier model id; $2/$10 per MTok in/out.

One thing I could NOT pin down from any doc: browser_evaluate's exact
parameter name (some sources suggest "function", one GitHub issue's stack
trace implies something else). Rather than guess and risk another silent
wrong-but-passing bug, tool_schema() below reads the REAL schema the running
server advertises and uses whatever its first required property is named.
If that guess is still wrong, the error message from the server will say so
explicitly (a loud failure, not a silent one).

CONFIRMED EMPIRICALLY (2026-09-20, from an actual run) and not documented
anywhere I could find: browser_evaluate's tool result is NOT the bare JS
return value. It comes back as a markdown-ish block:

    ### Result
    <json-encoded value>
    ### Ran Playwright code
    ```js
    <the code that ran>
    ```

_parse_evaluate_result() below pulls the value back out of that. Ground
truth checks were silently comparing against the WHOLE wrapper string
before this fix, so "actual: 0" (an int, correct) was failing against
"expected: 0" because it was really being compared as
"### Result\n0\n### Ran Playwright code\n..." != "0".

    uv add mcp anthropic
    # first run of `npx @playwright/mcp@latest` downloads it -- no separate
    # install step, but do a manual `npx @playwright/mcp@latest --version`
    # once so that first download doesn't eat into a timed run.

Requires ANTHROPIC_API_KEY in .env.
"""

import asyncio
import json
import os
import re
import time
from contextlib import AsyncExitStack

from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ANTHROPIC_MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5")

# One instruction per step, enforced as a system prompt rather than folded
# into the per-step user message -- a first run showed Claude sometimes
# replying with plain narration ("I'll now fill in the username...") and NO
# tool call on its second turn, which the loop (correctly) reads as "done,"
# so the action never actually happened. This makes the "always finish by
# acting, not narrating" rule persistent across every turn instead of
# hoping one user message conveys it.
AGENT_SYSTEM_PROMPT = (
    "You control a real browser through Playwright MCP tools. You are given "
    "exactly one concrete instruction per turn. Carry it out by actually "
    "performing the action (typing, clicking, selecting, checking, etc.) -- "
    "inspecting the page is not enough on its own. Call browser_snapshot "
    "first only if you need to see the current page to find the right "
    "element. Do not reply with only a plan, narration, or explanation and "
    "no tool call unless the instruction has genuinely already been carried "
    "out. When several elements share the same or a very similar accessible "
    "name (e.g. repeated row buttons or dropdowns), use nearby context -- "
    "which row or section an element sits in -- to pick the correct one; "
    "never guess or pick the first match by default."
)


def _parse_evaluate_result(raw_text):
    """Pull the actual JS return value back out of browser_evaluate's
    markdown-wrapped tool result (see module docstring). Falls back to the
    raw text if the expected markers aren't there, so a format change on
    the server's side degrades to 'comparison probably fails' rather than
    a crash."""
    if not isinstance(raw_text, str):
        return raw_text
    match = re.search(r"### Result\n(.*?)(?:\n### |\Z)", raw_text, re.DOTALL)
    value_str = match.group(1).strip() if match else raw_text.strip()
    try:
        return json.loads(value_str)
    except (json.JSONDecodeError, ValueError):
        return value_str

# platform.claude.com/docs/en/about-claude/pricing, checked 2026-09-19.
# Introductory pricing that became standard; verify again before trusting
# any cost figure this produces for real budgeting.
# Prices live in engines.PRICES (checked 2026-09-27) so the agent loop and
# the offline engines are costed from the same table.
# Safety cap on tool-call round-trips per atomic instruction, so a confused
# agent looping on the wrong element can't run forever.
MAX_TOOL_ITERATIONS = 8


def estimate_cost(input_tokens, output_tokens):
    from engines import price
    return price(ANTHROPIC_MODEL, {"input_tokens": input_tokens, "output_tokens": output_tokens})


class PlaywrightMCPClient:
    """Owns the real `npx @playwright/mcp@latest` subprocess and the MCP
    session talking to it. `anthropic_tools` is built from whatever the
    live server actually advertises -- never hardcoded -- so Claude sees
    the real tool set, params and all."""

    def __init__(self, headed=True):
        self.headed = headed
        self._stack = None
        self.session = None
        self._raw_tools = {}
        self.anthropic_tools = None

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        args = ["@playwright/mcp@latest"]
        if not self.headed:
            args.append("--headless")
        params = StdioServerParameters(command="npx", args=args)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

        tools_response = await self.session.list_tools()
        self._raw_tools = {t.name: t for t in tools_response.tools}
        self.anthropic_tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema,
            }
            for t in tools_response.tools
        ]
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    def tool_schema(self, name):
        t = self._raw_tools.get(name)
        return t.input_schema if t else None

    async def call_tool(self, name, arguments):
        result = await self.session.call_tool(name, arguments)
        parts = [getattr(b, "text", None) for b in result.content]
        parts = [p for p in parts if p]
        return "\n".join(parts) if parts else "(no text content returned)"

    async def evaluate(self, js_function):
        """Run a JS function against the page, for the HARNESS's own
        ground-truth checks -- kept separate from what Claude is asked to
        do (same role as the plain Playwright `page.evaluate` calls in
        flows.py's ground_truth_fn lambdas, just routed through the MCP
        tool instead of a direct Playwright handle, since the browser here
        lives inside the playwright-mcp subprocess, not in our process)."""
        schema = self.tool_schema("browser_evaluate") or {}
        required = schema.get("required") or []
        props = list((schema.get("properties") or {}).keys())
        param_name = required[0] if required else (props[0] if props else "function")
        raw = await self.call_tool("browser_evaluate", {param_name: js_function})
        return _parse_evaluate_result(raw)

    async def screenshot_b64(self):
        """A screenshot via the real browser_take_screenshot tool, for the
        dashboard's embedded split-screen view -- called by the harness
        itself, not by Claude, so it costs a tool round-trip but no LLM
        tokens. MCP's ImageContent blocks carry base64 in `.data` and a
        mime type in `.mimeType` per the protocol -- call_tool() above only
        extracts text blocks, so this reads the raw result directly.
        Best-effort: returns None on any failure rather than breaking the
        step that asked for it."""
        try:
            result = await self.session.call_tool("browser_take_screenshot", {})
            for block in result.content:
                data = getattr(block, "data", None)
                if getattr(block, "type", None) == "image" and data:
                    mime = getattr(block, "mimeType", "image/png")
                    return f"data:{mime};base64,{data}"
        except Exception:
            pass
        return None


async def run_step_with_claude(client, anthropic_client, step_num, description, instruction):
    """Runs ONE atomic instruction -- same granularity as a single
    flows.py action_step -- through a real Claude tool-use loop against
    the real playwright-mcp tools. Returns a dict shaped for the dashboard,
    with engine-specific metrics (tool_calls, tokens, cost) alongside the
    same step/description/latency_ms fields the Jev steps use."""
    messages = [{"role": "user", "content": f"Instruction: {instruction}"}]

    tool_calls = []
    input_tokens = 0
    output_tokens = 0
    start = time.monotonic()
    error = None

    for _ in range(MAX_TOOL_ITERATIONS):
        response = anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=AGENT_SYSTEM_PROMPT,
            tools=client.anthropic_tools,
            messages=messages,
        )
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_result_content = []
        for block in tool_use_blocks:
            tool_calls.append({"name": block.name, "input": block.input})
            try:
                output_text = await client.call_tool(block.name, block.input)
            except Exception as e:  # surfaced to Claude, not swallowed
                output_text = f"ERROR calling {block.name}: {e}"
                error = str(e)
            tool_result_content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output_text[:4000],
                }
            )
        messages.append({"role": "user", "content": tool_result_content})

        if response.stop_reason != "tool_use":
            break

    latency = time.monotonic() - start
    # A first live run showed Claude sometimes calling only read-only tools
    # (browser_snapshot etc.) for a whole step, then stopping without ever
    # acting -- the loop can't detect that from the API alone (stop_reason
    # looks like ordinary completion), so flag it heuristically for the
    # dashboard/summary to surface rather than silently reporting success.
    READ_ONLY_TOOLS = {
        "browser_snapshot", "browser_take_screenshot", "browser_console_messages",
        "browser_network_requests", "browser_network_request", "browser_find",
        "browser_tabs",
    }
    no_op_suspected = bool(tool_calls) and all(
        tc["name"] in READ_ONLY_TOOLS for tc in tool_calls
    )
    screenshot = await client.screenshot_b64()
    return {
        "step": step_num,
        "kind": "mcp_action",
        "description": description,
        "tool_calls": tool_calls,
        "tool_call_count": len(tool_calls),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(estimate_cost(input_tokens, output_tokens), 5),
        "latency_ms": round(latency * 1000),
        "error": error,
        "no_op_suspected": no_op_suspected,
        "screenshot": screenshot,
    }


async def mcp_verify(client, step_num, description, js_function, expected, check_fn=None):
    """A harness-side ground-truth check via browser_evaluate -- literal
    fact-checking, not an LLM judgment call, so it's tagged 'mcp_verify'
    rather than reusing Jev's 'verify' (Noul) kind, which IS an LLM call.
    Comparing these two kinds of "verify" directly would conflate two
    different things.

    check_fn, if given, overrides the default exact-match comparison --
    e.g. "does this string contain both of these substrings" for a check
    where there's no single expected literal value."""
    start = time.monotonic()
    raw = await client.evaluate(js_function)
    latency = time.monotonic() - start
    actual = raw.strip() if isinstance(raw, str) else raw
    correct = check_fn(actual) if check_fn else (str(actual) == str(expected))
    screenshot = await client.screenshot_b64()
    return {
        "step": step_num,
        "kind": "mcp_verify",
        "description": description,
        "expected": expected,
        "actual": actual,
        "correct": correct,
        "latency_ms": round(latency * 1000),
        "screenshot": screenshot,
    }


async def run_local_app_flow_mcp_steps(client, anthropic_client, base_url):
    """Claude+playwright-mcp equivalent of flows.run_local_app_flow_steps,
    same instructions/order/ground truths, so results are directly
    comparable step-for-step against the Jev-driven run."""
    # Navigation happens as harness setup, not a Claude-timed step -- same
    # convention as dashboard.py's page.goto() for the Jev flow, which
    # happens before that flow's loop starts.
    await client.call_tool("browser_navigate", {"url": f"{base_url}/test-app"})

    n = 0

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the username field",
        "Enter the username 'bryan' in the login form",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the password field",
        "Enter the password 'demo123' in the login form",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the login button",
        "Submit the login form",
    )

    n += 1
    yield await mcp_verify(
        client, n, "Delete buttons hidden for default role",
        "() => document.querySelectorAll('button.delete-btn').length",
        0,
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n,
        "find the role selector (not a per-ticket status dropdown)",
        "Switch the current role to Merchant",
    )

    n += 1
    yield await mcp_verify(
        client, n, "Delete buttons appear for Merchant role",
        "() => document.querySelectorAll('button.delete-btn').length",
        3,
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n,
        "find the Edit button for Marcus Webb's ticket "
        "(3 identical 'Edit' buttons on the page -- the real disambiguation test)",
        "Click the Edit button for the ticket belonging to Marcus Webb",
    )

    n += 1
    yield await mcp_verify(
        client, n, "correct row was edited (Marcus Webb)",
        "() => { const el = document.querySelector('tr.row-highlight'); "
        "return el ? el.getAttribute('data-customer') : null; }",
        "Marcus Webb",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n,
        "find the status dropdown for Alice Chen's ticket "
        "(3 identical 'Status' dropdowns -- same disambiguation test)",
        "Set the status dropdown to 'Closed' for Alice Chen's ticket",
    )

    n += 1
    yield await mcp_verify(
        client, n, "correct dropdown was changed (Alice Chen -> Closed)",
        "() => document.querySelector('tr[data-customer=\"Alice Chen\"] .status-select').value",
        "Closed",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the New Ticket button",
        "Open the form to create a new ticket",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n,
        "find the customer-name field (not the description textarea)",
        "Enter the customer name 'Devon Brooks' in the new ticket form",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the Store dropdown in the new ticket form",
        "Set the store to 'Harbor Knits' for the new ticket",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the Urgent checkbox",
        "Mark the new ticket as urgent",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the Submit button (not Cancel)",
        "Submit the new ticket form",
    )

    n += 1
    yield await mcp_verify(
        client, n, "new ticket created correctly",
        "() => { const el = document.querySelector('tr[data-customer=\"Devon Brooks\"]'); "
        "return el ? el.innerText.replace(/\\s+/g,' ').trim() : null; }",
        "row contains 'Devon Brooks' and 'Harbor Knits'",
        check_fn=lambda actual: bool(actual)
        and "Devon Brooks" in actual
        and "Harbor Knits" in actual,
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the role selector",
        "Switch the current role to Shopper",
    )

    n += 1
    yield await mcp_verify(
        client, n, "Delete buttons hidden again for Shopper role",
        "() => document.querySelectorAll('button.delete-btn').length",
        0,
    )


async def run_saucedemo_flow_mcp_steps(client, anthropic_client):
    """Claude+playwright-mcp equivalent of flows.run_saucedemo_flow_steps."""
    await client.call_tool("browser_navigate", {"url": "https://www.saucedemo.com/"})

    n = 0

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the username field",
        "Enter the username 'standard_user'",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the password field",
        "Enter the password 'secret_sauce'",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the login button",
        "Submit the login form",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n,
        "find the add-to-cart button for Sauce Labs Bike Light",
        "Add the 'Sauce Labs Bike Light' product to the cart (not any other product)",
    )

    n += 1
    yield await mcp_verify(
        client, n, "cart badge shows exactly 1 item",
        "() => { const el = document.querySelector('.shopping_cart_badge'); "
        "return el ? el.innerText : null; }",
        "1",
    )

    n += 1
    yield await run_step_with_claude(
        client, anthropic_client, n, "find the cart icon/link",
        "Open the shopping cart",
    )

    n += 1
    yield await mcp_verify(
        client, n, "cart contains exactly the Bike Light",
        "() => JSON.stringify(Array.from(document.querySelectorAll('.inventory_item_name'))"
        ".map(e => e.innerText))",
        '["Sauce Labs Bike Light"]',
    )


MCP_FLOWS = {
    "local_app": {
        "label": "Local ticket-admin app (disambiguation test)",
        "runner": "local_app",
    },
    "saucedemo": {
        "label": "SauceDemo (public demo site)",
        "runner": "saucedemo",
    },
}
