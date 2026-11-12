"""MCP event lifecycle, callback verification, filtering, retry and access tests."""
import asyncio
import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, hmac

from buzz_events import EVENT_NAME, EventError, EventService, callback_url, public_addresses
from gateway import create_app


class EventTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.json"
        self.now = 1794430000.0
        self.key = b"test-only-event-signing-key-12345"
        self.secret = "whsec_" + base64.b64encode(self.key).decode()
        self.state = {"channel_id": "bus-id", "channels": {"general": "general-id"},
                      "people": {"b" * 64: {"name": "Nate", "relationship": "owner"}}, "agents": {
            "dot": {"name": "Chief", "public_key": "a" * 64, "enabled": True,
                    "token_hash": "test-hash", "channels": ["general"]},
            "alpha": {"name": "alpha", "public_key": "d" * 64, "enabled": True, "token_hash": "other-hash"}}}
        self.rows, self.sent, self.statuses = [], [], []
        self.bad_challenge = False

        async def runner(agent, args):
            channel = args[args.index("--channel") + 1]
            since = int(args[args.index("--since") + 1])
            before = int(args[args.index("--before") + 1])
            limit = int(args[args.index("--limit") + 1])
            rows = [copy.deepcopy(r) for r in self.rows if r["channel_id"] == channel and since <= r["created_at"] <= before]
            return {"messages": sorted(rows, key=lambda r: (r["created_at"], r["id"]), reverse=True)[:limit]}

        self.runner = runner

        async def sender(url, body, headers):
            # Independently verify the Standard Webhooks signature with
            # cryptography, including the exact bytes and header metadata.
            signature = headers["webhook-signature"].split(" ")[0].split(",")[1]
            verifier = hmac.HMAC(self.key, hashes.SHA256())
            verifier.update((headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode() + body)
            verifier.verify(base64.b64decode(signature))
            payload = json.loads(body)
            if payload.get("type") == "verification":
                return 200, json.dumps({"challenge": "wrong" if self.bad_challenge else payload["challenge"]}).encode()
            self.sent.append((url, payload, headers.copy()))
            return self.statuses.pop(0) if self.statuses else 204, b""

        self.sender = sender
        self.service = self.new_service()

    def new_service(self):
        return EventService(lambda: copy.deepcopy(self.state), self.runner, self.path, self.sender, lambda: self.now)

    def params(self, **overrides):
        params = {"name": EVENT_NAME, "arguments": {"channel": "general", "mentions_only": True},
                  "delivery": {"mode": "webhook", "url": "https://callbacks.example/events/secret-path", "secret": self.secret}}
        params.update(overrides)
        return params

    def message(self, number, author="b", mentioned=True, channel="general-id"):
        self.rows.append({"id": f"{number:064x}", "pubkey": author * 64, "created_at": int(self.now),
                          "channel_id": channel, "content": f"test-only text {number}",
                          "tags": [["p", "a" * 64]] if mentioned else []})

    async def test_verified_subscription_filters_mentions_self_messages_and_channels(self):
        self.message(1)  # Existing messages must not wake a new subscription.
        subscribed = await self.service.rpc("events/subscribe", self.params(), "dot")
        self.now += 2
        self.message(2)
        self.message(3, mentioned=False)
        self.message(4, author="a")
        self.message(5, channel="bus-id")
        await self.service.tick()
        await self.service.tick()
        self.assertEqual(len(self.sent), 1)
        payload, headers = self.sent[0][1:]
        self.assertEqual(payload["data"]["message_id"], f"{2:064x}")
        self.assertEqual(payload["name"], EVENT_NAME)
        self.assertEqual(payload["data"]["author"]["name"], "Nate")
        self.assertEqual(payload["data"]["author"]["kind"], "human")
        self.assertEqual(payload["data"]["author"]["relationship"], "owner")
        self.assertEqual(headers["webhook-id"], payload["eventId"])
        self.assertEqual(headers["X-MCP-Subscription-Id"], subscribed["id"])
        self.assertNotIn("type", payload)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        restarted = self.new_service()
        await restarted.tick()
        self.assertEqual(len(self.sent), 1)
        status = json.dumps(restarted.status("dot"))
        self.assertNotIn(self.secret, status)
        self.assertNotIn("secret-path", status)

    async def test_retry_survives_restart_uses_same_event_id_and_fresh_signature(self):
        await self.service.rpc("events/subscribe", self.params(), "dot")
        self.now += 2
        self.message(6)
        self.statuses = [503, 204]
        await self.service.tick()
        self.assertEqual(self.service.status("dot")["subscriptions"][0]["pending_deliveries"], 1)
        restarted = self.new_service()
        self.now += 3
        await restarted.tick()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[0][1], self.sent[1][1])
        self.assertNotEqual(self.sent[0][2]["webhook-timestamp"], self.sent[1][2]["webhook-timestamp"])
        self.assertEqual(restarted.status("dot")["subscriptions"][0]["pending_deliveries"], 0)

    async def test_persistent_subscription_survives_time_and_restart_and_remains_revocable(self):
        created = await self.service.rpc("events/subscribe", self.params(), "dot")
        self.assertIsNone(created["refreshBefore"])
        self.assertIsNone(self.service.status("dot")["subscriptions"][0]["expires_at"])
        restarted = self.new_service()
        self.now += 30 * 86400
        self.message(900)
        await restarted.tick()
        self.assertTrue(restarted.status("dot")["subscriptions"][0]["active"])
        self.assertEqual(len(self.sent), 1)
        self.state["agents"]["dot"]["enabled"] = False
        self.now += 2
        self.message(901)
        await restarted.tick()
        self.assertFalse(restarted.status("dot")["subscriptions"][0]["active"])
        self.assertEqual(len(self.sent), 1)

    async def test_null_ttl_converts_finite_subscription_without_changing_its_identity(self):
        finite = await self.service.rpc("events/subscribe", self.params(ttlMs=60_000), "dot")
        self.assertIsNotNone(finite["refreshBefore"])
        persistent = await self.service.rpc("events/subscribe", self.params(ttlMs=None), "dot")
        self.assertEqual(finite["id"], persistent["id"])
        self.assertIsNone(persistent["refreshBefore"])
        self.assertEqual(len(self.service.data["subscriptions"]), 1)

    async def test_burst_at_one_timestamp_is_paged_and_delivered_without_duplicates(self):
        await self.service.rpc("events/subscribe", self.params(), "dot")
        self.now += 2
        for number in range(1000, 1201):
            self.message(number)
        for _ in range(11):
            await self.service.tick()
        self.assertEqual(len(self.sent), 201)
        self.assertEqual(len({payload["eventId"] for _, payload, _ in self.sent}), 201)

    async def test_idempotent_refresh_secret_rotation_and_unsubscribe(self):
        first = await self.service.rpc("events/subscribe", self.params(), "dot")
        refreshed = await self.service.rpc("events/subscribe", self.params(arguments={"mentions_only": True, "channel": "general"}), "dot")
        self.assertEqual(first["id"], refreshed["id"])
        self.assertEqual(len(self.service.data["subscriptions"]), 1)
        self.key = b"replacement-test-only-secret-12345"
        self.secret = "whsec_" + base64.b64encode(self.key).decode()
        await self.service.rpc("events/subscribe", self.params(), "dot")
        self.now += 2
        self.message(7)
        await self.service.tick()
        self.assertEqual(len(self.sent[0][2]["webhook-signature"].split(" ")), 2)
        params = self.params()
        del params["delivery"]["secret"]
        await self.service.rpc("events/unsubscribe", params, "dot")
        await self.service.rpc("events/unsubscribe", params, "dot")
        self.now += 2
        self.message(8)
        await self.service.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.service.status("dot")["subscriptions"], [])

    async def test_revocation_and_expiration_stop_delivery(self):
        await self.service.rpc("events/subscribe", self.params(ttlMs=60_000), "dot")
        self.now += 2
        self.message(9)
        self.state["agents"]["dot"]["enabled"] = False
        await self.service.tick()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.service.status("dot")["subscriptions"][0]["active"])
        self.state["agents"]["dot"]["enabled"] = True
        await self.service.rpc("events/subscribe", self.params(ttlMs=60_000), "dot")
        self.now += 61
        self.message(10)
        await self.service.tick()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.service.status("dot")["subscriptions"][0]["active"])

    async def test_callback_rejection_and_invalid_filters_create_no_subscription(self):
        self.bad_challenge = True
        with self.assertRaises(EventError) as failure:
            await self.service.rpc("events/subscribe", self.params(), "dot")
        self.assertEqual(failure.exception.code, -32015)
        self.bad_challenge = False
        with self.assertRaises(EventError):
            await self.service.rpc("events/subscribe", self.params(), "alpha")
        with self.assertRaises(EventError):
            await self.service.rpc("events/subscribe", self.params(arguments={"channel": "general", "mentions_only": "false"}), "dot")
        with self.assertRaises(EventError):
            await self.service.rpc("events/subscribe", self.params(delivery={"mode": "webhook", "url": "https://callbacks.example/e", "secret": "whsec_bad"}), "dot")
        self.assertEqual(self.service.data["subscriptions"], {})

    async def test_all_messages_mode_still_excludes_own_and_gone_callbacks_stop(self):
        await self.service.rpc("events/subscribe", self.params(arguments={"channel": "general", "mentions_only": False}), "dot")
        self.now += 2
        self.message(11, mentioned=False)
        self.message(12, author="a", mentioned=False)
        self.statuses = [410]
        await self.service.tick()
        self.now += 2
        self.message(13)
        await self.service.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.service.status("dot")["subscriptions"][0]["active"])

    async def test_unsubscribe_can_finish_while_a_callback_is_in_flight(self):
        await self.service.rpc("events/subscribe", self.params(), "dot")
        self.now += 2
        self.message(14)
        self.message(15)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_sender(url, body, headers):
            started.set()
            await release.wait()
            return await self.sender(url, body, headers)

        self.service.sender = slow_sender
        task = asyncio.create_task(self.service.tick())
        await asyncio.wait_for(started.wait(), 1)
        try:
            await asyncio.wait_for(self.service.rpc("events/unsubscribe", self.params(), "dot"), .5)
        finally:
            release.set()
            await task
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.service.data["subscriptions"], {})

    async def test_mcp_event_methods_share_auth_and_reject_untrusted_origins(self):
        import hashlib
        self.state["agents"]["dot"]["token_hash"] = hashlib.sha256(b"test-only-dot").hexdigest()
        app = create_app(lambda: copy.deepcopy(self.state), self.runner, self.service)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as http:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "events/list", "params": {}}
                self.assertEqual((await http.post("/mcp", json=payload)).status_code, 401)
                headers = {"Authorization": "Bearer test-only-dot"}
                result = await http.post("/mcp", json=payload, headers=headers)
                self.assertEqual(result.json()["result"]["events"][0]["name"], EVENT_NAME)
                result = await http.post("/mcp", json=payload, headers={**headers, "Origin": "https://untrusted.example"})
                self.assertEqual(result.status_code, 403)
                payload.update(method="events/subscribe", params=self.params())
                result = await http.post("/mcp", json=payload, headers=headers)
                self.assertIn("id", result.json()["result"])

    def test_callback_url_and_dns_checks_block_local_private_and_mixed_destinations(self):
        for url in ("http://callback.example/e", "https://127.0.0.1/e", "https://[::1]/", "https://user:password@callback.example/e", "https://callback.example:8443/e", "https://callback.example/e#fragment"):
            with self.assertRaises(EventError):
                callback_url(url)
        for addresses in (["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"], ["224.0.0.1"], ["1.1.1.1", "10.0.0.1"]):
            resolver = lambda *a, **k: [(None, None, None, None, (ip, 443)) for ip in addresses]
            with self.assertRaises(EventError):
                public_addresses("callbacks.example", resolver)


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
