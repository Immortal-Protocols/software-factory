"""Local configuration and the deliberately narrow Buzz CLI bridge."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state.json"
CLI = Path("/Applications/Buzz Local.app/Contents/MacOS/buzz")
RELAY = "http://localhost:3000"
PORT = 8766


def load_state() -> dict:
    if STATE.stat().st_mode & 0o077:
        raise RuntimeError("MCP credential state must have permissions 600")
    return json.loads(STATE.read_text())


def agent_channels(state: dict, agent: dict) -> dict[str, str]:
    """Resolve only named channels explicitly granted to this identity."""
    channels = {"agent-bus": state["channel_id"]}
    for name in agent.get("channels", []):
        if name != "agent-bus" and name in state.get("channels", {}):
            channels[name] = state["channels"][name]
    return channels


def author_identity(state: dict, public_key: str) -> dict:
    """Resolve exact keys from owner configuration, never from message text."""
    person = state.get("people", {}).get(public_key)
    if person:
        return {"public_key": public_key, "name": person["name"], "kind": "human",
                "relationship": person.get("relationship", "unknown"), "verified": True,
                "identity_source": "owner_configured"}
    for agent in state.get("agents", {}).values():
        if agent["public_key"] == public_key:
            return {"public_key": public_key, "name": agent["name"], "kind": "agent",
                    "relationship": "agent", "verified": True, "identity_source": "gateway_identity"}
    return {"public_key": public_key, "name": None, "kind": "unknown",
            "relationship": "unknown", "verified": False, "identity_source": "unknown"}


def enrich_messages(state: dict, result):
    def enrich(row):
        return {**row, "author": author_identity(state, row.get("pubkey", ""))}
    if isinstance(result, list):
        return [enrich(row) for row in result]
    if isinstance(result, dict) and isinstance(result.get("messages"), list):
        return {**result, "messages": [enrich(row) for row in result["messages"]]}
    return result


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cli_env(identity: dict) -> dict:
    # Do not inherit credentials or agent-provider settings from the caller.
    return {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
        "BUZZ_RELAY_URL": RELAY,
        "BUZZ_PRIVATE_KEY": identity["private_key"],
        "NO_PROXY": "localhost,127.0.0.1",
    }


def cli(identity: dict, *args: str, content: str | None = None):
    result = subprocess.run(
        [str(CLI), "--format", "json", *args],
        input=content,
        text=True,
        capture_output=True,
        env=cli_env(identity),
        timeout=25,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Buzz request failed")[:1000])
    return json.loads(result.stdout) if result.stdout.strip() else {}
