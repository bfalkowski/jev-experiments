"""Run the Claude + playwright-mcp agent loop (condition A) end to end.

This is the live baseline: Claude drives a real browser through the
playwright-mcp tools, one instruction per step, on the same flows the
dashboard runs. Each step's tokens, cost, latency and tool calls are logged,
along with the harness's ground-truth checks, to results/agent.jsonl.

Examples:
  AGENT_MODEL=claude-opus-5-5 python run_agent.py --flows local_app --runs 1 --max-usd 3
  AGENT_MODEL=claude-opus-5-5 python run_agent.py --flows local_app,saucedemo --runs 2 --max-usd 6
"""

import argparse
import asyncio
import functools
import http.server
import json
import os
import threading
import uuid
from datetime import datetime, timezone

from typesafe_client import load_dotenv

load_dotenv()

from anthropic import Anthropic  # noqa: E402

import mcp_playwright_agent as agent  # noqa: E402

PORT = 8766


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] in ("/test-app", "/test-app/"):
            self.path = "/local_test_app.html"
        return super().do_GET()

    def log_message(self, *a):
        pass


def serve_local_app():
    handler = functools.partial(_Handler, directory=os.path.dirname(os.path.abspath(__file__)))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{PORT}"


async def run_once(flow, base_url, anthropic_client, out, run_id, spent, max_usd):
    async with agent.PlaywrightMCPClient(headed=False) as client:
        if flow == "local_app":
            steps = agent.run_local_app_flow_mcp_steps(client, anthropic_client, base_url)
        else:
            steps = agent.run_saucedemo_flow_mcp_steps(client, anthropic_client)
        async for step in steps:
            step.pop("screenshot", None)
            row = {"run_id": run_id, "flow": flow, "model": agent.ANTHROPIC_MODEL,
                   "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **step}
            out.write(json.dumps(row) + "\n")
            out.flush()
            spent += step.get("cost_usd", 0.0) or 0.0
            tag = step["kind"]
            extra = (f"ok={step.get('correct')}" if tag == "mcp_verify"
                     else f"calls={step.get('tool_call_count')} ${step.get('cost_usd')}")
            print(f"  {flow} step {step['step']:>2} {tag:<10} {extra}  total ${spent:.3f}")
            if spent >= max_usd:
                print(f"budget cap reached: ${spent:.3f}")
                break
    return spent


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flows", default="local_app,saucedemo")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--max-usd", type=float, required=True)
    ap.add_argument("--out", default="results/agent.jsonl")
    args = ap.parse_args()

    base_url = serve_local_app()
    anthropic_client = Anthropic()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    spent = 0.0
    with open(args.out, "a") as out:
        for r in range(args.runs):
            for flow in [f.strip() for f in args.flows.split(",") if f.strip()]:
                if spent >= args.max_usd:
                    break
                run_id = uuid.uuid4().hex[:8]
                print(f"run {r + 1}/{args.runs} {flow} ({agent.ANTHROPIC_MODEL}) id={run_id}")
                spent = await run_once(flow, base_url, anthropic_client, out, run_id, spent, args.max_usd)
    print(f"spent this run: ${spent:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
