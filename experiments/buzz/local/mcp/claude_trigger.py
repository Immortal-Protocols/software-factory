"""Configure and invoke the one Claude cloud task used for Buzz replies."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from common import ROOT, agent_channels, cli, load_state, private_json

WATCH_ID = "local_claude_general_mentions"
FIRE_URL = re.compile(r"https://api\.anthropic\.com/v1/claude_code/routines/trig_[A-Za-z0-9]+/fire")
SESSION_URL = re.compile(r"https://claude\.ai/code/(?:session|cse)_[A-Za-z0-9]+")


def validate_credential(value):
    url, token = value.get("fire_url"), value.get("token")
    if not isinstance(url, str) or not FIRE_URL.fullmatch(url):
        raise ValueError("Expected the Claude task's exact api.anthropic.com fire URL")
    if not isinstance(token, str) or not re.fullmatch(r"sk-ant-[A-Za-z0-9_-]{25,1000}", token):
        raise ValueError("Expected the task's private trigger token")
    return url, token


def fire_request(subscription, event=None, test_only=False):
    url, token = validate_credential({"fire_url": subscription["url"], "token": subscription["routine_token"]})
    context = {"source": "buzz", "test_only": True} if test_only else {"source": "buzz", "event": event}
    body = json.dumps({"text": json.dumps(context, separators=(",", ":"), ensure_ascii=False)},
                      separators=(",", ":"), ensure_ascii=False).encode()
    if len(body) > 64 * 1024:
        raise ValueError("Claude trigger context is too large")
    return url, body, {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                       "anthropic-version": "2026-10-21"}


def session_from_response(body):
    result = json.loads(body)
    url = result.get("claude_code_session_url")
    if result.get("type") != "routine_fire" or not isinstance(url, str) or not SESSION_URL.fullmatch(url):
        raise ValueError("Claude did not return a valid cloud session")
    return url


def retry_delay(headers):
    value = {k.lower(): v for k, v in headers.items()}.get("retry-after", "60")
    try:
        return max(60, min(86400, int(value)))
    except (ValueError, TypeError):
        return 3600


async def configure(path):
    from control import owned_pid, runtime
    from buzz_events import EventService, messages_from
    if owned_pid(runtime(), "gateway"):
        raise RuntimeError("Stop the managed gateway before installing the trigger configuration")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Trigger credential file must have permissions 600")
    credential = json.loads(path.read_text())
    url, token = validate_credential(credential)
    state = load_state()
    agent = state["agents"]["remote-beta"]
    if not agent.get("enabled") or "general" not in agent_channels(state, agent):
        raise RuntimeError("Claude must be enabled and allowed in general")
    owners = [key for key, person in state.get("people", {}).items() if person.get("relationship") == "owner"]
    if len(owners) != 1:
        raise RuntimeError("An exact owner public key is required")

    async def runner(identity, args):
        return await asyncio.to_thread(cli, identity, *args)

    events_path = ROOT / ".events.json"
    service = EventService(load_state, runner, events_path)
    previous = service.data["subscriptions"].get(WATCH_ID)
    now = time.time()
    watch = {"id": WATCH_ID, "delivery_type": "claude_routine", "agent_id": "remote-beta",
             "token_hash": agent["token_hash"], "channel": "general", "mentions_only": True,
             "mention_text": "claude", "author_public_key": owners[0], "url": url,
             "routine_token": token, "expires_at": None, "active": True, "started_at": int(now),
             "scan_from": int(now), "seen": [], "pending": {}}
    if previous and previous["url"] == url and previous["token_hash"] == agent["token_hash"]:
        for key in ("started_at", "scan_from", "seen", "pending", "last_delivery_at", "last_session_url", "uncertain_events"):
            if key in previous:
                watch[key] = previous[key]
    else:
        rows = await service.read_window(agent, agent_channels(state, agent)["general"], max(0, int(now) - 2), now)
        watch["seen"] = [r["id"] for r in messages_from(rows)]
    service.data["subscriptions"][WATCH_ID] = watch
    service.save()
    print(json.dumps({"configured": True, "agent": "claude", "channel": "general",
                      "owner_mentions_only": True, "expires_at": None}))


async def test_trigger(path):
    from buzz_events import pinned_post
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Trigger credential file must have permissions 600")
    url, token = validate_credential(json.loads(path.read_text()))
    target, body, headers = fire_request({"url": url, "routine_token": token}, test_only=True)
    status, response = await asyncio.to_thread(pinned_post, target, body, headers)
    if status != 200:
        raise RuntimeError(f"Claude trigger test failed with HTTP {status}; credential kept private")
    session_url = session_from_response(response)
    proof = {"api_trigger_ok": True, "read_only_test_session": session_url}
    (ROOT / "claude-trigger-verification.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps(proof))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["configure", "test"])
    parser.add_argument("credential_file", type=Path)
    args = parser.parse_args()
    asyncio.run(configure(args.credential_file) if args.action == "configure" else test_trigger(args.credential_file))
