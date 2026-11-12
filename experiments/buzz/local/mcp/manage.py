#!/usr/bin/env python3
"""Manage local MCP identities and private client connection files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

from common import ROOT, STATE, cli, load_state, private_json
from routing import allowed_public_endpoint, sync_agents


def identity(name: str) -> dict:
    key = ec.generate_private_key(ec.SECP256K1())
    return {
        "name": name,
        "private_key": f"{key.private_numbers().private_value:064x}",
        "public_key": f"{key.public_key().public_numbers().x:064x}",
    }


def bootstrap() -> dict:
    if STATE.exists():
        state = load_state()
        if state.get("channel_id"):
            return state
    else:
        state = {"version": 1, "owner": identity("buzz-mcp-gateway"), "agents": {}}
    private_json(STATE, state)
    cli(state["owner"], "users", "set-profile", "--name", "buzz-mcp-gateway",
        "--about", "Local gateway for remote MCP agents; scoped to the agent-bus channel.")
    channel = cli(state["owner"], "channels", "create", "--name", "agent-bus",
                  "--type", "stream", "--visibility", "open",
                  "--description", "Coordination channel for authenticated remote MCP agents.")
    print(json.dumps({"created_channel": channel}))
    # Buzz CLI returns the created channel as an object with its UUID.
    if isinstance(channel, dict):
        state["channel_id"] = channel.get("id") or channel.get("channel_id")
        if not state["channel_id"] and isinstance(channel.get("channel"), dict):
            state["channel_id"] = channel["channel"].get("id")
    if not state.get("channel_id"):
        raise RuntimeError("Could not resolve created channel UUID; inspect channel output")
    private_json(STATE, state)
    return state


def add_agent(name: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
        raise ValueError("Use a lowercase name: a-z, 0-9 and hyphens; at most 40 characters")
    state = load_state()
    if name in state["agents"]:
        raise ValueError("This agent already exists")
    agent = identity(name)
    token = "buzz_" + secrets.token_urlsafe(32)
    agent.update(token_hash=hashlib.sha256(token.encode()).hexdigest(), enabled=True)
    cli(agent, "users", "set-profile", "--name", name,
        "--about", "Remote agent identity using the authenticated Buzz MCP gateway.")
    cli(agent, "channels", "join", "--channel", state["channel_id"])
    state["agents"][name] = agent
    private_json(STATE, state)
    connection = {
        "mcpServers": {"buzz": {"url": state.get("endpoint", f"http://127.0.0.1:8766/mcp"),
                                 "headers": {"Authorization": "Bearer " + token}}}
    }
    private_json(ROOT / "connections" / f"{name}.json", connection)
    sync_agents(state)
    print(f"Created {name}; private connection file: connections/{name}.json")


def set_endpoint(url: str) -> None:
    if not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com/mcp", url) and not allowed_public_endpoint(url):
        raise ValueError("Expected the generated tunnel URL or the configured fixed gateway")
    state = load_state()
    state["endpoint"] = url
    private_json(STATE, state)
    for path in (ROOT / "connections").glob("*.json"):
        connection = json.loads(path.read_text())
        connection["mcpServers"]["buzz"]["url"] = url
        private_json(path, connection)
    (ROOT / "endpoint.txt").write_text(url + "\n")
    print(url)


def allow_channel(agent_name: str, channel_name: str) -> None:
    """Grant one existing named channel after joining it as the agent."""
    state = load_state()
    agent = state["agents"][agent_name]
    if not agent.get("enabled"):
        raise ValueError("Agent is revoked")
    channel_name = channel_name.removeprefix("#")
    matches = [c for c in cli(state["owner"], "channels", "list") if c["name"] == channel_name]
    if len(matches) != 1:
        raise ValueError("Expected exactly one existing channel with that name")
    channel_id = matches[0]["channel_id"]
    cli(agent, "channels", "join", "--channel", channel_id)
    state.setdefault("channels", {})[channel_name] = channel_id
    granted = agent.setdefault("channels", ["agent-bus"])
    if channel_name not in granted:
        granted.append(channel_name)
    private_json(STATE, state)
    print(f"Granted {agent_name} read/post access to #{channel_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("bootstrap")
    sub.add_parser("list-agents")
    add = sub.add_parser("add-agent")
    add.add_argument("name")
    revoke = sub.add_parser("revoke-agent")
    revoke.add_argument("name")
    endpoint = sub.add_parser("set-endpoint")
    endpoint.add_argument("url")
    allow = sub.add_parser("allow-channel")
    allow.add_argument("agent")
    allow.add_argument("channel")
    args = parser.parse_args()
    if args.action == "bootstrap":
        bootstrap()
    elif args.action == "add-agent":
        add_agent(args.name)
    elif args.action == "revoke-agent":
        state = load_state()
        state["agents"][args.name]["enabled"] = False
        private_json(STATE, state)
        sync_agents(state)
        print(f"Revoked {args.name}; its bearer token is now rejected")
    elif args.action == "set-endpoint":
        set_endpoint(args.url)
    elif args.action == "allow-channel":
        allow_channel(args.agent, args.channel)
    else:
        state = load_state()
        print(json.dumps({name: {"public_key": a["public_key"], "enabled": a["enabled"]}
                          for name, a in state["agents"].items()}, indent=2))


if __name__ == "__main__":
    main()
