"""Register the changing tunnel behind the fixed, token-protected public front door."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from common import ROOT, load_state

ROUTING = ROOT / ".routing.json"


def settings():
    if not ROUTING.exists():
        return None
    if ROUTING.stat().st_mode & 0o077:
        raise RuntimeError("Routing credentials must have permissions 600")
    config = json.loads(ROUTING.read_text())
    return config if config.get("enabled") else None


def allowed_public_endpoint(url):
    config = settings()
    return bool(config and url == config["stable_endpoint"])


def register(upstream, *, state=None):
    config = settings()
    if config is None:
        return upstream
    if not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com/mcp", upstream):
        raise ValueError("Expected the current Quick Tunnel MCP origin")
    stable = urlsplit(config["stable_endpoint"])
    registration = urlsplit(config["registration_url"])
    if stable.scheme != "https" or registration.scheme != "https" or stable.netloc != registration.netloc:
        raise ValueError("Routing registration must use the fixed gateway's HTTPS origin")
    state = load_state() if state is None else state
    hashes = [a["token_hash"] for a in state["agents"].values() if a.get("enabled")]
    with httpx.Client(trust_env=False, timeout=20) as client:
        response = client.post(config["registration_url"],
            headers={"Authorization": "Bearer " + config["registration_key"]},
            json={"upstream": upstream, "token_hashes": hashes})
        response.raise_for_status()
        if response.json().get("registered") is not True:
            raise RuntimeError("Fixed gateway did not confirm the routing update")
    return config["stable_endpoint"]


def sync_agents(state):
    if settings() is None:
        return
    runtime = json.loads((ROOT / ".runtime.json").read_text())
    upstream = runtime.get("upstream_endpoint")
    if not upstream:
        raise RuntimeError("Run control.py start to synchronize the fixed gateway")
    register(upstream, state=state)
