"""Persistent MCP Events subscriptions and signed, filtered Buzz notifications."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from starlette.responses import JSONResponse

from common import agent_channels, author_identity, private_json
from claude_trigger import fire_request, retry_delay, session_from_response

EVENT_NAME = "buzz.message.created"


def event_definition(channels):
    return {
        "name": EVENT_NAME,
        "description": "A new Buzz message in a permitted channel. By default only mentions of your identity are delivered. Your own messages are always excluded to prevent reply loops.",
        "delivery": ["webhook"],
        "inputSchema": {"type": "object", "properties": {
            "channel": {"type": "string", "enum": list(channels), "description": "Buzz channel name to monitor."},
            "mentions_only": {"type": "boolean", "default": True, "description": "Deliver only messages mentioning this Buzz identity."}},
            "required": ["channel"], "additionalProperties": False},
        "payloadSchema": {"type": "object", "properties": {
            "channel": {"type": "string"}, "channel_id": {"type": "string"},
            "message_id": {"type": "string"}, "author_public_key": {"type": "string"},
            "text": {"type": "string"}, "mentioned": {"type": "boolean"},
            "author": {"type": "object", "description": "Identity mapped by exact signing key. Owner-configured Nate is the human owner; unknown keys remain unknown.",
                "properties": {"public_key": {"type": "string"}, "name": {"type": ["string", "null"]},
                    "kind": {"enum": ["human", "agent", "unknown"]}, "relationship": {"type": "string"},
                    "verified": {"type": "boolean"}, "identity_source": {"type": "string"}},
                "required": ["public_key", "name", "kind", "relationship", "verified", "identity_source"],
                "additionalProperties": False}},
            "required": ["channel", "channel_id", "message_id", "author_public_key", "text", "mentioned"],
            "additionalProperties": False},
    }


class EventError(Exception):
    def __init__(self, message, code=-32602, reason=None):
        super().__init__(message)
        self.code, self.reason = code, reason


def callback_url(value):
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
            raise ValueError()
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.port not in (None, 443) or parts.username or parts.password or parts.fragment:
            raise ValueError()
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host or host.endswith("."):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        return urlunsplit(("https", host, parts.path or "/", parts.query, ""))
    except (ValueError, TypeError, UnicodeError):
        raise EventError("Callback must be a public HTTPS hostname on port 443") from None


def decode_secret(value):
    if not isinstance(value, str) or not value.startswith("whsec_") or len(value) > 120:
        raise EventError("Expected a whsec_ signing secret")
    try:
        encoded = value[6:]
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        if not 24 <= len(decoded) <= 64:
            raise ValueError()
        return decoded
    except ValueError:
        raise EventError("Signing secret must decode to 24–64 bytes") from None


def signed_headers(sub, event_id, body, now):
    timestamp = str(int(now))
    material = event_id.encode() + b"." + timestamp.encode() + b"." + body
    keys = [sub["secret"]]
    if sub.get("old_secret") and sub.get("old_secret_until", 0) > now:
        keys.append(sub["old_secret"])
    signatures = ["v1," + base64.b64encode(hmac.digest(decode_secret(k), material, "sha256")).decode() for k in keys]
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": timestamp, "webhook-signature": " ".join(signatures),
            "X-MCP-Subscription-Id": sub["id"]}


def public_addresses(host, resolver=socket.getaddrinfo):
    addresses = list(dict.fromkeys(row[4][0] for row in resolver(host, 443, type=socket.SOCK_STREAM)))
    # Validate every address. The selected address is pinned through the TLS
    # connection, so a second DNS lookup cannot redirect it to a private host.
    if not addresses or any(not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast for a in addresses):
        raise EventError("Callback address is not public", -32015, "non_public_address")
    return addresses


def pinned_post(url, body, headers, include_headers=False):
    parts = urlsplit(callback_url(url))
    host = parts.hostname
    address = public_addresses(host)[0]
    context = ssl.create_default_context()

    class PinnedConnection(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((address, 443), timeout=10)
            try:
                self.sock = context.wrap_socket(sock, server_hostname=host)
            except BaseException:
                sock.close()
                raise

    connection = PinnedConnection(host, timeout=10)
    try:
        # HTTPConnection emits Host for the original hostname. No redirects or
        # proxy environment variables are consulted.
        connection.request("POST", parts.path + ("?" + parts.query if parts.query else ""), body, headers)
        response = connection.getresponse()
        response_body = response.read(65537)
        if len(response_body) > 65536:
            raise EventError("Callback response is too large", -32015, "response_too_large")
        if include_headers:
            return response.status, response_body, dict(response.getheaders())
        return response.status, response_body
    finally:
        connection.close()


async def webhook_post(url, body, headers):
    return await asyncio.to_thread(pinned_post, url, body, headers)


def messages_from(result):
    rows = result.get("messages", []) if isinstance(result, dict) else result
    if not isinstance(rows, list):
        raise ValueError("Invalid Buzz messages")
    return rows


def iso_timestamp(seconds):
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def expired(sub, now):
    return sub["expires_at"] is not None and sub["expires_at"] <= now


class EventService:
    def __init__(self, state_loader, runner, path: Path | None = None, sender=webhook_post, clock=time.time, routine_sender=None):
        self.state_loader, self.runner, self.path = state_loader, runner, path
        self.sender, self.clock = sender, clock
        self.routine_sender = routine_sender or self.post_routine
        self.lock = asyncio.Lock()
        self.tick_lock = asyncio.Lock()
        self.running = False
        self.last_poll = None
        self.poll_error = None
        self.data = {"version": 1, "subscriptions": {}}
        if path and path.exists():
            if path.stat().st_mode & 0o077:
                raise RuntimeError("Event subscription state must have permissions 600")
            self.data = json.loads(path.read_text())

    def save(self):
        if self.path:
            private_json(self.path, self.data)

    async def post_routine(self, url, body, headers):
        return await asyncio.to_thread(pinned_post, url, body, headers, True)

    def authorized(self, state, sub):
        agent = state["agents"].get(sub["agent_id"])
        return bool(agent and agent.get("enabled") and agent.get("token_hash") == sub["token_hash"]
                    and sub["channel"] in agent_channels(state, agent))

    async def read_window(self, agent, channel_id, since, until):
        """Page a bounded time window without dropping a timestamp boundary."""
        rows, before = {}, int(until)
        for _ in range(10):
            args = ["messages", "get", "--channel", channel_id, "--limit", "100", "--kinds", "9",
                    "--since", str(since), "--before", str(before)]
            batch = messages_from(await self.runner(agent, args))
            for row in batch:
                rows[row["id"]] = row
            if len(batch) < 100:
                return sorted(rows.values(), key=lambda r: (r["created_at"], r["id"]))
            oldest = min(row["created_at"] for row in batch)
            # Fetch the complete cutoff second before moving to an older page.
            boundary = messages_from(await self.runner(agent, ["messages", "get", "--channel", channel_id,
                "--limit", "500", "--kinds", "9", "--since", str(oldest), "--before", str(oldest)]))
            if len(boundary) >= 500:
                raise ValueError("Message burst exceeds the per-second scan limit")
            for row in boundary:
                rows[row["id"]] = row
            before = oldest - 1
            if before < since:
                return sorted(rows.values(), key=lambda r: (r["created_at"], r["id"]))
        raise ValueError("Message window exceeds the scan limit; watermark retained")

    def identity(self, state, agent_id, params):
        agent = state["agents"][agent_id]
        if params.get("name") != EVENT_NAME:
            raise EventError("Unknown event")
        args = params.get("arguments", {})
        if not isinstance(args, dict) or set(args) - {"channel", "mentions_only"}:
            raise EventError("Invalid event arguments")
        channel = args.get("channel")
        if not isinstance(channel, str) or channel not in agent_channels(state, agent):
            raise EventError("Channel is not permitted")
        mentions = args.get("mentions_only", True)
        if not isinstance(mentions, bool):
            raise EventError("mentions_only must be boolean")
        delivery = params.get("delivery", {})
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise EventError("Only webhook delivery is supported")
        url = callback_url(delivery.get("url"))
        identity = {"agent_id": agent_id, "url": url, "name": EVENT_NAME,
                    "arguments": {"channel": channel, "mentions_only": mentions}}
        sub_id = "buzz_sub_" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return sub_id, agent, channel, mentions, url

    async def rpc(self, method, params, agent_id):
        async with self.lock:
            state = self.state_loader()
            agent = state["agents"].get(agent_id)
            if not agent or not agent.get("enabled"):
                raise EventError("Agent access was revoked", -32001)
            if method == "events/list":
                return {"events": [event_definition(agent_channels(state, agent))]}
            if method not in {"events/subscribe", "events/unsubscribe"}:
                raise EventError("Method not found", -32601)
            if not isinstance(params, dict):
                raise EventError("Invalid event parameters")
            sub_id, agent, channel, mentions, url = self.identity(state, agent_id, params)
            subscriptions = self.data["subscriptions"]
            if method == "events/unsubscribe":
                subscriptions.pop(sub_id, None)
                self.save()
                return {}
            secret = params.get("delivery", {}).get("secret")
            decode_secret(secret)
            ttl_ms = params.get("ttlMs")  # Omitted/null requests the persistent default.
            if ttl_ms is not None and (isinstance(ttl_ms, bool) or not isinstance(ttl_ms, (int, float)) or not 0 < ttl_ms <= 2**53):
                raise EventError("ttlMs must be a positive duration or null")
            now = self.clock()
            previous = subscriptions.get(sub_id)
            if previous and (not previous["active"] or expired(previous, now) or not self.authorized(state, previous)):
                previous = None
            if not previous and len(subscriptions) >= 20:
                raise EventError("Subscription limit reached")
            sub = {"id": sub_id, "agent_id": agent_id, "token_hash": agent["token_hash"],
                   "channel": channel, "mentions_only": mentions, "url": url, "secret": secret,
                   "expires_at": None if ttl_ms is None else now + min(7 * 86400, max(60, ttl_ms / 1000)), "active": True,
                   "started_at": int(now), "scan_from": int(now), "seen": [], "pending": {}}
            if previous:
                for key in ("started_at", "scan_from", "seen", "pending", "last_delivery_at", "last_error"):
                    if key in previous:
                        sub[key] = previous[key]
                if previous["secret"] != secret:
                    sub.update(old_secret=previous["secret"], old_secret_until=now + 300)
                elif previous.get("old_secret_until", 0) > now:
                    sub.update(old_secret=previous["old_secret"], old_secret_until=previous["old_secret_until"])
            cached = next((s for s in subscriptions.values() if s["agent_id"] == agent_id and s["url"] == url
                and s.get("secret") == secret and s.get("verified_until", 0) > now and self.authorized(state, s)), None)
            if cached:
                sub["verified_until"] = cached["verified_until"]
            else:
                challenge = secrets.token_urlsafe(32)
                challenge_body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode()
                try:
                    status, body = await asyncio.wait_for(self.sender(url, challenge_body,
                        signed_headers(sub, "verification_" + secrets.token_hex(16), challenge_body, now)), timeout=12)
                    echoed = json.loads(body).get("challenge")
                    if not 200 <= status < 300 or not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
                        raise EventError("Callback verification failed", -32015, "challenge_failed")
                except EventError:
                    raise
                except (TimeoutError, OSError, ValueError, AttributeError, http.client.HTTPException):
                    raise EventError("Callback verification failed", -32015, "timeout_or_invalid_response") from None
                sub["verified_until"] = self.clock() + 300
            if not previous:
                # Snapshot existing message IDs, so starting a watch does not
                # fire on old messages. Subsequent reads overlap timestamps.
                channel_id = agent_channels(state, agent)[channel]
                rows = await asyncio.wait_for(self.read_window(agent, channel_id,
                    max(0, sub["started_at"] - 2), self.clock()), timeout=8)
                sub["seen"] = [row["id"] for row in rows if isinstance(row.get("id"), str)]
            # Revocation during callback verification must not create a lease.
            if not self.authorized(self.state_loader(), sub):
                raise EventError("Agent access was revoked", -32001)
            subscriptions[sub_id] = sub
            self.save()
            return {"id": sub_id, "refreshBefore": iso_timestamp(sub["expires_at"]), "cursor": None, "truncated": False}

    def status(self, agent_id):
        now = self.clock()
        return {"event": EVENT_NAME, "watcher_running": self.running,
                "poll_interval_seconds": 3, "last_poll_at": self.last_poll,
                "poll_error": self.poll_error, "subscriptions": [
                    {"id": s["id"], "channel": s["channel"], "mentions_only": s["mentions_only"],
                     "delivery_type": s.get("delivery_type", "webhook"),
                     "active": bool(s["active"] and not expired(s, now)),
                     "expires_at": iso_timestamp(s["expires_at"]), "pending_deliveries": len(s["pending"]),
                     "last_delivery_at": s.get("last_delivery_at"), "last_error": s.get("last_error"),
                     "last_session_url": s.get("last_session_url"), "uncertain_deliveries": len(s.get("uncertain_events", []))}
                    for s in self.data["subscriptions"].values() if s["agent_id"] == agent_id]}

    async def tick(self):
        async with self.tick_lock:
            await self.scan()
            await self.deliver_pending()

    async def scan(self):
        async with self.lock:
            state, now = self.state_loader(), self.clock()
            groups = {}
            changed = False
            for sub in self.data["subscriptions"].values():
                if sub["active"] and (not self.authorized(state, sub) or expired(sub, now)):
                    sub.update(active=False, pending={}, last_error="access_revoked_or_expired")
                    changed = True
                if sub["active"]:
                    groups.setdefault((sub["agent_id"], sub["channel"]), []).append(sub)
            for (agent_id, channel), subscriptions in groups.items():
                agent = state["agents"][agent_id]
                channel_id = agent_channels(state, agent)[channel]
                since = max(0, min(s["scan_from"] for s in subscriptions) - 2)
                rows = await asyncio.wait_for(self.read_window(agent, channel_id, since, now), timeout=10)
                for sub in subscriptions:
                    seen = set(sub["seen"])
                    full = False
                    for row in rows:
                        event_id, author, created = row.get("id"), row.get("pubkey"), row.get("created_at")
                        if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{64}", event_id):
                            continue
                        if event_id in seen or not isinstance(created, (int, float)) or created < sub["started_at"]:
                            continue
                        mentioned = any(isinstance(t, list) and len(t) > 1 and t[0] == "p" and t[1] == agent["public_key"] for t in row.get("tags", []))
                        if sub.get("mention_text"):
                            mentioned = mentioned or bool(re.search(r"(?<![\w-])@" + re.escape(sub["mention_text"]) + r"(?![\w-])", str(row.get("content", "")), re.IGNORECASE))
                        allowed_author = not sub.get("author_public_key") or author == sub["author_public_key"]
                        if author != agent["public_key"] and allowed_author and (mentioned or not sub["mentions_only"]):
                            if len(sub["pending"]) >= 1000:
                                full = True
                                break
                            sub["pending"][event_id] = {"attempt": 0, "next_at": now, "event": {
                                "eventId": "buzz_" + event_id, "name": EVENT_NAME,
                                "timestamp": iso_timestamp(created), "cursor": None,
                                "data": {"channel": channel, "channel_id": channel_id, "message_id": event_id,
                                         "author_public_key": author, "author": author_identity(state, author),
                                         "text": str(row.get("content", ""))[:8000], "mentioned": mentioned}}}
                        seen.add(event_id)
                        sub["seen"].append(event_id)
                    sub["seen"] = sub["seen"][-4000:]
                    if not full:
                        sub["scan_from"] = int(now)
                    # A full queue retains its scan watermark for follow-up.
                    # Persist the queue before a network delivery is attempted.
                    changed = True
            self.last_poll = iso_timestamp(now)
            self.poll_error = None
            if changed:
                self.save()

    async def deliver_pending(self):
        async with self.lock:
            batches = [(s["id"], list(s["pending"])[:20]) for s in self.data["subscriptions"].values() if s["active"]]
        for sub_id, event_ids in batches:
            for event_id in event_ids:
                async with self.lock:
                    sub = self.data["subscriptions"].get(sub_id)
                    if not sub or not sub["active"]:
                        break
                    pending = sub["pending"].get(event_id)
                    if not pending or pending["next_at"] > self.clock():
                        continue
                    if not self.authorized(self.state_loader(), sub) or expired(sub, self.clock()):
                        sub.update(active=False, pending={}, last_error="access_revoked_or_expired")
                        self.save()
                        break
                    event = pending["event"]
                    if sub.get("delivery_type") == "claude_routine":
                        routine = True
                        if pending.get("in_flight"):
                            self.uncertain(sub, event_id)
                            continue
                        url, body, headers = fire_request(sub, event)
                        # The fire API has no idempotency key. Persist before
                        # POST and never retry an ambiguous network outcome.
                        pending["in_flight"] = True
                        self.save()
                    else:
                        routine = False
                        body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()
                        url = sub["url"]
                        headers = signed_headers(sub, event["eventId"], body, self.clock())
                # A slow callback must not lock out unsubscribe or refresh.
                try:
                    if routine:
                        status, response_body, response_headers = await asyncio.wait_for(self.routine_sender(url, body, headers), timeout=12)
                    else:
                        status, _ = await asyncio.wait_for(self.sender(url, body, headers), timeout=12)
                except (OSError, TimeoutError, EventError, http.client.HTTPException):
                    status = None if routine else 503
                async with self.lock:
                    sub = self.data["subscriptions"].get(sub_id)
                    if not sub or not sub["active"] or event_id not in sub["pending"]:
                        break
                    pending = sub["pending"][event_id]
                    if routine:
                        if status is not None and 200 <= status < 300:
                            try:
                                session_url = session_from_response(response_body)
                            except (ValueError, TypeError, AttributeError):
                                self.uncertain(sub, event_id)
                                continue
                            del sub["pending"][event_id]
                            sub.update(last_session_url=session_url, last_delivery_at=iso_timestamp(self.clock()), last_error=None)
                        elif status == 429:
                            pending["in_flight"] = False
                            pending["attempt"] += 1
                            pending["next_at"] = self.clock() + retry_delay(response_headers)
                            sub["last_error"] = "claude_rate_limited"
                        elif status is None or status >= 500:
                            self.uncertain(sub, event_id)
                            continue
                        else:
                            del sub["pending"][event_id]
                            sub["last_error"] = f"claude_trigger_rejected_{status}"
                            if status in (401, 403, 404):
                                sub.update(active=False, pending={})
                        self.save()
                        if not sub["active"]:
                            break
                        continue
                    if 200 <= status < 300:
                        del sub["pending"][event_id]
                        sub["last_delivery_at"] = iso_timestamp(self.clock())
                        sub["last_error"] = None
                    elif status == 410:
                        sub.update(active=False, pending={}, last_error="callback_gone")
                    elif status == 429 or status >= 500:
                        pending["attempt"] += 1
                        sub["last_error"] = "transient_delivery_failure"
                        if pending["attempt"] >= 8:
                            del sub["pending"][event_id]
                            sub["last_error"] = "retries_exhausted"
                        else:
                            pending["next_at"] = self.clock() + min(300, 2 ** pending["attempt"])
                    else:
                        del sub["pending"][event_id]
                        sub["last_error"] = "callback_rejected_payload"
                    self.save()
                    if not sub["active"]:
                        break

    def uncertain(self, sub, event_id):
        sub["pending"].pop(event_id, None)
        sub.setdefault("uncertain_events", []).append(event_id)
        sub["uncertain_events"] = sub["uncertain_events"][-100:]
        sub["last_error"] = "claude_trigger_outcome_unknown_no_automatic_retry"
        self.save()

    async def run(self):
        self.running = True
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    # Do not log callback URLs, signing secrets, or message data.
                    self.poll_error = "buzz_poll_or_delivery_failed"
                await asyncio.sleep(3)
        finally:
            self.running = False


class EventRpcMiddleware:
    def __init__(self, app, events, current):
        self.app, self.events, self.current = app, events, current

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        if headers.get(b"host", b"").decode() not in {"127.0.0.1:8766", "localhost:8766"} or (
            headers.get(b"origin") and headers[b"origin"].decode() not in {"http://127.0.0.1:8766", "http://localhost:8766"}):
            await JSONResponse({"error": "untrusted_origin"}, status_code=403)(scope, receive, send)
            return
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            chunks.append(chunk)
            size += len(chunk)
            if size > 64 * 1024:
                await JSONResponse({"error": "request_too_large"}, status_code=413)(scope, receive, send)
                return
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("method"), str) and payload["method"].startswith("events/"):
            try:
                if payload.get("jsonrpc") != "2.0" or "id" not in payload:
                    raise EventError("Invalid request", -32600)
                _, _, agent_id = self.current()
                result = await self.events.rpc(payload["method"], payload.get("params", {}), agent_id)
                response = {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
            except EventError as error:
                detail = {"code": error.code, "message": str(error)}
                if error.reason:
                    detail["data"] = {"reason": error.reason}
                response = {"jsonrpc": "2.0", "id": payload.get("id"), "error": detail}
            except Exception:
                response = {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32603, "message": "Event service unavailable"}}
            await JSONResponse(response, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
