#!/usr/bin/env python3
"""Start, inspect, or stop the private gateway and its public Quick Tunnel."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import time

import httpx

from common import PORT, ROOT, load_state, private_json
from manage import set_endpoint
from routing import register

RUNTIME = ROOT / ".runtime.json"
PYTHON = ROOT / ".venv/bin/python"


def runtime() -> dict:
    return json.loads(RUNTIME.read_text()) if RUNTIME.exists() else {}


def owned_pid(state: dict, name: str) -> int | None:
    pid = state.get(name + "_pid")
    if not isinstance(pid, int) or pid < 2:
        return None
    process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    expected = str(ROOT / "gateway.py") if name == "gateway" else f"--url http://127.0.0.1:{PORT}"
    return pid if expected in process.stdout else None


def spawn(name: str, command: list[str], state: dict) -> None:
    log = ROOT / f".{name}.log"
    fd = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    state[name + "_pid"] = process.pid
    private_json(RUNTIME, state)


def start() -> None:
    if not load_state().get("channel_id"):
        raise RuntimeError("Initialize the dedicated Buzz channel first")
    subprocess.run([str(ROOT.parent / "buzz.sh"), "start"], check=True)
    state = runtime()
    if not owned_pid(state, "gateway"):
        spawn("gateway", [str(PYTHON), str(ROOT / "gateway.py")], state)
    with httpx.Client(trust_env=False, timeout=5) as client:
        for _ in range(30):
            try:
                response = client.get(f"http://127.0.0.1:{PORT}/health")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Gateway did not start; inspect .gateway.log")
    if not owned_pid(state, "tunnel"):
        spawn("tunnel", ["/opt/homebrew/bin/cloudflared", "tunnel", "--no-autoupdate",
                         "--protocol", "http2", "--url", f"http://127.0.0.1:{PORT}",
                         "--http-host-header", f"127.0.0.1:{PORT}",
                         "--metrics", "127.0.0.1:20246", "--management-diagnostics=false",
                         "--grace-period", "5s"], state)
    print("Waiting for the public HTTPS address…", flush=True)
    for _ in range(90):
        log = (ROOT / ".tunnel.log").read_text(errors="replace")
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", log)
        if match:
            upstream = match.group(0) + "/mcp"
            # Wait for the newly assigned tunnel to route successfully before
            # moving the fixed public front door to it.
            ready = False
            readiness_deadline = time.monotonic() + 120
            with httpx.Client(trust_env=False, timeout=4) as client:
                while time.monotonic() < readiness_deadline:
                    try:
                        response = client.get(match.group(0) + "/health")
                        if response.status_code == 200 and response.json().get("status") == "ready":
                            ready = True
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(2)
            if not ready:
                raise RuntimeError("New tunnel DNS or routing is not ready; retry start shortly")
            state["upstream_endpoint"] = upstream
            private_json(RUNTIME, state)
            endpoint = register(upstream)
            set_endpoint(endpoint)
            state["endpoint"] = endpoint
            private_json(RUNTIME, state)
            return
        if not owned_pid(state, "tunnel"):
            raise RuntimeError("Tunnel stopped; inspect .tunnel.log")
        time.sleep(1)
    raise RuntimeError("Tunnel address not ready; inspect .tunnel.log")


def stop() -> None:
    state = runtime()
    stopping = []
    for name in ("tunnel", "gateway"):
        pid = owned_pid(state, name)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                stopping.append(name)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 15
    while any(owned_pid(state, name) for name in stopping):
        if time.monotonic() > deadline:
            raise RuntimeError("A managed process is still shutting down; retry stop shortly")
        time.sleep(0.1)
    for name in ("tunnel", "gateway"):
        state.pop(name + "_pid", None)
    private_json(RUNTIME, state)
    print("MCP gateway and public tunnel stopped. Local Buzz data and credentials are preserved.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    if args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    else:
        state = runtime()
        print(json.dumps({"endpoint": state.get("endpoint"),
                          "gateway_running": bool(owned_pid(state, "gateway")),
                          "tunnel_running": bool(owned_pid(state, "tunnel"))}, indent=2))


if __name__ == "__main__":
    main()
