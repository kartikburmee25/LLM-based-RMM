#!/usr/bin/env python3
"""
Small debugging agent for the RMM control plane.

A plain terminal loop: you type a question, Claude decides whether it needs
to call the control plane's API to answer it (list devices, check a job,
run a diagnostic script), the tool result goes back to Claude, and it keeps
going until it has a real answer -- which gets printed as text. Nothing
fancy: no streaming, no memory beyond one run, no GUI.

Setup
-----
    pip install -r requirements.txt

    export ANTHROPIC_API_KEY="sk-ant-..."     # your Claude API key
    export RMM_BASE_URL="http://192.168.0.144:8000"   # your control plane
    export RMM_API_KEY="key_..."              # a control-plane API key

    python agent.py

Then just ask things like:
    > list all devices and tell me which ones are unreachable
    > device abc123 hasn't run a job in a while, what's going on?
    > run "Get-Service | Where-Object Status -ne 'Running'" on device abc123

Safety: the run_script tool always prints the exact script and asks you
(the human) to confirm before it's sent to a real machine. Claude can
propose a script; it can't execute one without you saying yes.
"""

import json
import os
import sys
import time

import requests
from anthropic import Anthropic

# ---- Configuration ---------------------------------------------------------

RMM_BASE_URL = os.environ.get("RMM_BASE_URL", "http://192.168.0.144:8000").rstrip("/")
RMM_API_KEY = os.environ.get("RMM_API_KEY", "")
# A real, known-good model id. If your account has access to something
# newer, override it: export ANTHROPIC_MODEL="claude-..."
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
MAX_TOOL_TURNS = 8  # hard stop so a confused loop can't run forever

if not RMM_API_KEY:
    sys.exit("Set RMM_API_KEY to a control-plane API key before running this.")

client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

SYSTEM_PROMPT = f"""You are a debugging assistant for a self-hosted RMM
control plane at {RMM_BASE_URL}. You have tools to inspect devices and jobs
and, with the human's explicit confirmation, run a diagnostic PowerShell
script on a device.

Useful things to know about this system when diagnosing issues:
- A device is "reachable" only while its agent's WebSocket connection to
  the control plane is open. If it's unreachable, no job can be dispatched
  to it -- jobs just sit "pending" until either the device reconnects or
  the job's timeout elapses, at which point it becomes "device_unreachable".
- Job status "timed_out" means the job WAS dispatched but the agent never
  reported a result in time (script hung, or the agent/device dropped
  mid-run) -- different root cause than "device_unreachable".
- Always check device reachability before assuming a script/job problem --
  most "my job never finishes" reports trace back to the device being
  offline.
- Be concise and concrete. State what you checked and what you found before
  proposing next steps.
"""

TOOLS = [
    {
        "name": "list_devices",
        "description": "List all enrolled devices with hostname, enrollment time, last-seen time, and whether each is reachable right now.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_device",
        "description": "Get one device's details by id.",
        "input_schema": {
            "type": "object",
            "properties": {"device_id": {"type": "string"}},
            "required": ["device_id"],
        },
    },
    {
        "name": "list_jobs_for_device",
        "description": "List jobs (script executions) for one device, most recent first, with status/exit code/duration for each.",
        "input_schema": {
            "type": "object",
            "properties": {"device_id": {"type": "string"}},
            "required": ["device_id"],
        },
    },
    {
        "name": "get_job",
        "description": "Get one job's full status and result (stdout/stderr/exit code/duration) by id.",
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "run_script",
        "description": (
            "Dispatch a raw PowerShell script to a device and wait for it to reach a "
            "terminal status. Use this to actively diagnose a machine (e.g. check a "
            "service, read an event log). The human operator is always asked to "
            "confirm the exact script before anything runs on the real device."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "script": {"type": "string", "description": "Raw PowerShell content."},
                "timeout_seconds": {"type": "integer", "description": "Optional, defaults to the server default."},
            },
            "required": ["device_id", "script"],
        },
    },
]


# ---- Control-plane HTTP client ---------------------------------------------

def rmm_request(method: str, path: str, **kwargs) -> dict:
    url = f"{RMM_BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {RMM_API_KEY}"}
    headers.update(kwargs.pop("headers", {}))
    try:
        resp = requests.request(method, url, headers=headers, timeout=5, **kwargs)
    except requests.exceptions.RequestException as e:
        # This is itself useful diagnostic signal (server down, wrong URL,
        # network unreachable) -- hand it to the model as data, not a crash.
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    return {"ok": resp.ok, "status_code": resp.status_code, "body": body}


# ---- Tool implementations ---------------------------------------------------

def tool_list_devices(_: dict) -> dict:
    return rmm_request("GET", "/devices")


def tool_get_device(args: dict) -> dict:
    return rmm_request("GET", f"/devices/{args['device_id']}")


def tool_list_jobs_for_device(args: dict) -> dict:
    return rmm_request("GET", f"/devices/{args['device_id']}/jobs")


def tool_get_job(args: dict) -> dict:
    return rmm_request("GET", f"/jobs/{args['job_id']}")


def tool_run_script(args: dict) -> dict:
    device_id = args["device_id"]
    script = args["script"]
    timeout_seconds = args.get("timeout_seconds")

    print("\n--- Claude wants to run a script ---")
    print(f"device: {device_id}")
    print("script:")
    print(script)
    print("-------------------------------------")
    answer = input("Run this on the real device? [y/N] ").strip().lower()
    if answer != "y":
        return {"ok": False, "error": "human declined to run this script"}

    payload = {"script": script}
    if timeout_seconds:
        payload["timeout_seconds"] = timeout_seconds

    created = rmm_request("POST", f"/devices/{device_id}/jobs", json=payload)
    if not created.get("ok"):
        return created
    job_id = created["body"]["id"]
    deadline = time.time() + (timeout_seconds or 300) + 15

    print(f"dispatched job {job_id}, waiting for it to finish ...")
    while time.time() < deadline:
        result = rmm_request("GET", f"/jobs/{job_id}")
        status = result.get("body", {}).get("status")
        if status not in ("pending", "dispatched"):
            return result
        time.sleep(2)
    return {"ok": False, "error": "gave up waiting locally; check the job id later", "job_id": job_id}


TOOL_IMPLS = {
    "list_devices": tool_list_devices,
    "get_device": tool_get_device,
    "list_jobs_for_device": tool_list_jobs_for_device,
    "get_job": tool_get_job,
    "run_script": tool_run_script,
}


# ---- Agent loop --------------------------------------------------------------

def fmt_elapsed(seconds: float) -> str:
    return f"{seconds * 1000:.0f}ms" if seconds < 1 else f"{seconds:.2f}s"


def run_turn(messages: list) -> None:
    """One user question -> zero or more tool calls -> printed text answer."""

    turn_start = time.perf_counter()
    claude_calls = 0
    tool_calls = 0

    for _ in range(MAX_TOOL_TURNS):
        step_start = time.perf_counter()
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        claude_calls += 1
        print(f"\n[claude response in {fmt_elapsed(time.perf_counter() - step_start)}]")

        # Print any text Claude produced this turn (it may think out loud
        # before or alongside a tool call).
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\nclaude: {block.text.strip()}")

        if response.stop_reason != "tool_use":
            break  # done -- final answer already printed above

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            impl = TOOL_IMPLS.get(block.name)
            print(f"\n[calling {block.name}({json.dumps(block.input)})]")
            tool_start = time.perf_counter()
            result = impl(block.input) if impl else {"ok": False, "error": f"unknown tool {block.name}"}
            tool_calls += 1
            elapsed = time.perf_counter() - tool_start
            print(f"[{block.name} finished in {fmt_elapsed(elapsed)}: {json.dumps(result)[:500]}]")
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})
    else:
        print("\nclaude: (stopped after too many tool calls in one turn -- ask a narrower question)")

    print(
        f"\n[turn finished in {fmt_elapsed(time.perf_counter() - turn_start)} "
        f"-- {claude_calls} claude call(s), {tool_calls} tool call(s)]"
    )


def main() -> None:
    print(f"RMM debug agent -- control plane at {RMM_BASE_URL}. Ctrl+C to quit.\n")
    messages: list = []
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        messages.append({"role": "user", "content": question})
        run_turn(messages)


if __name__ == "__main__":
    main()
