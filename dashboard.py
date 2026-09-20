"""
Local web dashboard for the Jev browser-action prototype. Replaces
reading terminal output: open http://127.0.0.1:8000/, pick a flow, hit
Run, and watch step-by-step results (element picked, confidence,
latency, ground-truth pass/fail) render as cards while the headless
browser drives itself in the background.

Also runs the SAME flows through a genuine Claude (Anthropic API) +
real microsoft/playwright-mcp tool-use loop (mcp_playwright_agent.py),
as a directly comparable "engine" alongside the Jev-driven one -- pick
the engine in the dropdown next to the flow.

    uv add flask playwright mcp anthropic
    uv run python dashboard.py

Then open http://127.0.0.1:8000/ in your browser (not "localhost" -- see
the BASE_URL comment below for why). The Claude+MCP engine
needs ANTHROPIC_API_KEY in .env, and Node/npm available (it launches
`npx @playwright/mcp@latest` as a subprocess on first use).
"""

import asyncio
import json
from pathlib import Path

from anthropic import Anthropic
from flask import Flask, Response, request
from playwright.sync_api import sync_playwright

from flows import FLOWS
from mcp_playwright_agent import (
    MCP_FLOWS,
    PlaywrightMCPClient,
    run_local_app_flow_mcp_steps,
    run_saucedemo_flow_mcp_steps,
)
from typesafe_client import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
PORT = 8000
# Literal IPv4 address, not "localhost": on a run, /test-app 404'd with the
# raw http.server error page (not Flask's own 404), and the failing
# requests came from ::1 (IPv6) while the working ones came from 127.0.0.1
# -- something else on the machine is listening on port 8000 over IPv6,
# and "localhost" was resolving to it some of the time. Pinning to the
# literal IPv4 address sidesteps that ambiguity instead of debugging
# whatever else is squatting on the port.
BASE_URL = f"http://127.0.0.1:{PORT}"

HERE = Path(__file__).parent
TEST_APP_HTML = HERE / "local_test_app.html"
DASHBOARD_HTML = HERE / "dashboard.html"


@app.route("/test-app")
def test_app():
    """Serves the local ticket-admin test app on the SAME server as the
    dashboard, so there's only one process to run."""
    return TEST_APP_HTML.read_text()


@app.route("/")
def index():
    options = "".join(
        f'<option value="{key}">{cfg["label"]}</option>' for key, cfg in FLOWS.items()
    )
    html = DASHBOARD_HTML.read_text()
    return html.replace("{{FLOW_OPTIONS}}", options)


def _jev_flow_steps(flow_key, headed):
    """Plain sync generator for the Jev engine -- pulled out of the /run
    route so it can be driven either alone or concurrently alongside the
    MCP engine (see _run_both). Each step dict now carries an embedded
    'screenshot' (see flows.py's _screenshot_b64) instead of the browser
    popping up as its own OS window -- headed still optionally ALSO opens
    a real window (for anyone who wants both), but the embedded screenshot
    is what actually replaces "launch a new tab" with "see the site's
    state on this page." """
    cfg = FLOWS.get(flow_key)
    if cfg is None:
        raise ValueError(f"unknown flow: {flow_key}")

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not headed,
                slow_mo=400 if headed else 0,
            )
            page = browser.new_page()
            url = cfg["url"]
            if url.startswith("/"):
                url = BASE_URL + url
            page.goto(url)
            for step in cfg["runner"](page):
                yield step
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def _mcp_flow_steps_async(flow_key, headed):
    """The async Claude+playwright-mcp flow as a single async generator
    function (not yet bridged to sync) -- factored out so both
    _run_mcp_engine (standalone) and _run_both (concurrent) can drive it
    inside their own single asyncio Task, which is what the cancel-scope
    fix below depends on."""

    async def agen():
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            yield {
                "__error__": "ANTHROPIC_API_KEY not set in .env -- the "
                "Claude+playwright-mcp engine needs it (same .env the Jev "
                "engine reads TYPESAFE_API_KEY from)."
            }
            return

        anthropic_client = Anthropic(api_key=api_key)

        # This engine's slowest, least visible phase is BEFORE step 1:
        # launching `npx @playwright/mcp@latest` cold, spinning up a
        # browser, and completing the MCP handshake -- easily longer than
        # Jev's entire 16-step run. Without a status event here, the
        # split-screen column just sits blank that whole time, which looks
        # exactly like it hasn't started (not like it's mid-startup).
        yield {
            "kind": "status",
            "message": "Launching npx @playwright/mcp@latest and connecting "
            "(cold start can take a while)...",
        }
        client = PlaywrightMCPClient(headed=headed)
        try:
            await client.__aenter__()
        except Exception as e:
            yield {"__error__": f"failed to start playwright-mcp: {e}"}
            return

        try:
            yield {
                "kind": "status",
                "message": f"Connected -- {len(client.anthropic_tools)} tools available.",
            }
            if flow_key == "local_app":
                async for step in run_local_app_flow_mcp_steps(client, anthropic_client, BASE_URL):
                    yield step
            elif flow_key == "saucedemo":
                async for step in run_saucedemo_flow_mcp_steps(client, anthropic_client):
                    yield step
            else:
                yield {"__error__": f"unknown flow: {flow_key}"}
        finally:
            await client.__aexit__(None, None, None)

    return agen


def _run_mcp_engine(flow_key, headed):
    """Bridges the async Claude+playwright-mcp flow into a plain sync
    generator /run can consume like any other engine.

    First attempt at this drove the async generator's __anext__() one step
    at a time via repeated loop.run_until_complete() calls on a bare event
    loop. That crashed at the end of a real run with "Attempted to exit
    cancel scope in a different task than it was entered in": each
    run_until_complete() call wraps the coroutine in a NEW asyncio Task,
    but the mcp SDK's stdio transport uses anyio cancel scopes internally,
    and anyio requires a scope to be entered and exited within the SAME
    Task. Splitting one logical async-with (PlaywrightMCPClient's session
    lifecycle) across many different Tasks broke that invariant.

    Fix: run the ENTIRE async flow -- from client connect to client
    teardown -- as one coroutine in one Task, on a dedicated background
    thread with its own event loop, and forward each yielded step to this
    (synchronous, Flask-generator) thread through a plain queue.Queue."""
    import queue
    import threading

    q = queue.Queue()
    agen = _mcp_flow_steps_async(flow_key, headed)

    def worker():
        async def consume():
            try:
                async for item in agen():
                    q.put(("item", item))
            except Exception as e:
                q.put(("error", str(e)))

        try:
            asyncio.run(consume())
        finally:
            q.put(("done", None))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while True:
        kind, payload = q.get()
        if kind == "item":
            yield payload
        elif kind == "error":
            raise RuntimeError(payload)
        else:  # "done"
            break
    thread.join()


def _run_both(flow_key, headed):
    """Runs the Jev engine and the Claude+playwright-mcp engine
    CONCURRENTLY against the same flow -- each in its own thread, each
    driving its own separate browser -- and merges their step events into
    one stream, tagged by 'engine', so the dashboard can render both
    columns filling in side by side in real time instead of running one
    engine after the other."""
    import queue
    import threading

    q = queue.Queue()

    def jev_worker():
        q.put(("item", {
            "kind": "status", "engine": "jev",
            "message": "Launching a real Chromium browser via Playwright...",
        }))
        try:
            for step in _jev_flow_steps(flow_key, headed):
                step["engine"] = "jev"
                q.put(("item", step))
        except Exception as e:
            q.put(("error", ("jev", str(e))))
        finally:
            q.put(("engine_done", "jev"))

    def mcp_worker():
        agen = _mcp_flow_steps_async(flow_key, headed)

        async def consume():
            try:
                async for step in agen():
                    if "__error__" in step:
                        q.put(("error", ("mcp", step["__error__"])))
                    else:
                        step["engine"] = "mcp"
                        q.put(("item", step))
            except Exception as e:
                q.put(("error", ("mcp", str(e))))

        try:
            asyncio.run(consume())
        finally:
            q.put(("engine_done", "mcp"))

    t1 = threading.Thread(target=jev_worker, daemon=True)
    t2 = threading.Thread(target=mcp_worker, daemon=True)
    t1.start()
    t2.start()

    finished = set()
    while len(finished) < 2:
        kind, payload = q.get()
        if kind == "item":
            yield payload
        elif kind == "error":
            engine, message = payload
            yield {"__error__": message, "engine": engine}
        else:  # "engine_done"
            finished.add(payload)
    t1.join()
    t2.join()


@app.route("/run")
def run_flow():
    flow_key = request.args.get("flow", "local_app")
    headed = request.args.get("headed", "false").lower() == "true"
    engine = request.args.get("engine", "jev")

    def emit_step(step):
        return f"event: step\ndata: {json.dumps(step)}\n\n"

    def emit_error(message, engine_tag=None):
        payload = {"message": message}
        if engine_tag:
            payload["engine"] = engine_tag
        return f"event: error_event\ndata: {json.dumps(payload)}\n\n"

    def generate():
        if engine == "both":
            if flow_key not in FLOWS or flow_key not in MCP_FLOWS:
                yield emit_error(f"unknown flow: {flow_key}")
                yield "event: done\ndata: {}\n\n"
                return
            try:
                for step in _run_both(flow_key, headed):
                    if "__error__" in step:
                        yield emit_error(step["__error__"], step.get("engine"))
                    else:
                        yield emit_step(step)
            except Exception as e:
                yield emit_error(str(e))
            finally:
                yield "event: done\ndata: {}\n\n"
            return

        if engine == "mcp":
            if flow_key not in MCP_FLOWS:
                yield emit_error(f"unknown flow: {flow_key}")
                yield "event: done\ndata: {}\n\n"
                return
            try:
                for step in _run_mcp_engine(flow_key, headed):
                    if "__error__" in step:
                        yield emit_error(step["__error__"])
                    else:
                        yield emit_step(step)
            except Exception as e:
                yield emit_error(str(e))
            finally:
                yield "event: done\ndata: {}\n\n"
            return

        if flow_key not in FLOWS:
            yield emit_error(f"unknown flow: {flow_key}")
            yield "event: done\ndata: {}\n\n"
            return
        try:
            for step in _jev_flow_steps(flow_key, headed):
                yield emit_step(step)
        except Exception as e:
            yield emit_error(str(e))
        finally:
            yield "event: done\ndata: {}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    print(f"Dashboard running at {BASE_URL}/")
    # host pinned to the literal IPv4 loopback, matching BASE_URL above --
    # open the dashboard itself at 127.0.0.1:8000, not localhost:8000, to
    # avoid the same IPv6-resolution ambiguity.
    app.run(host="127.0.0.1", port=PORT, threaded=True)
