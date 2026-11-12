"""Authentication and channel boundaries, exercised through real MCP requests with a fake relay."""
import copy
import hashlib
import json
import logging
import unittest
from contextlib import asynccontextmanager
from functools import wraps

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gateway import create_app


def running_server(method):
    @wraps(method)
    async def wrapped(self):
        async with self.app.router.lifespan_context(self.app):
            await method(self)
    return wrapped


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = {"channel_id": "11111111-1111-4111-8111-111111111111", "agents": {}}
        for name, token, letter in [("alpha", "test-only-alpha", "a"), ("beta", "test-only-beta", "b")]:
            self.state["agents"][name] = {"name": name, "public_key": letter * 64,
                "private_key": "test-only-unused", "enabled": True,
                "token_hash": hashlib.sha256(token.encode()).hexdigest()}
        self.calls = []
        self.messages = []

        async def runner(identity, args, content=None):
            self.calls.append((identity["name"], args, content))
            return copy.deepcopy(self.messages) if args[1] == "get" else {"accepted": True, "event_id": "c" * 64}

        self.app = create_app(lambda: copy.deepcopy(self.state), runner)

    @asynccontextmanager
    async def client(self, token):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                    headers={"Authorization": "Bearer " + token}) as http:
            async with streamable_http_client("http://127.0.0.1:8766/mcp", http_client=http) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    yield session

    @running_server
    async def test_missing_invalid_revoked_and_untrusted_origin(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                    base_url="http://127.0.0.1:8766") as http:
            self.assertEqual((await http.post("/mcp", json={})).status_code, 401)
            self.assertEqual((await http.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})).status_code, 401)
            headers = {"Authorization": "Bearer test-only-alpha", "Origin": "https://untrusted.example"}
            self.assertEqual((await http.post("/mcp", json={}, headers=headers)).status_code, 403)
            self.state["agents"]["alpha"]["enabled"] = False
            self.assertEqual((await http.post("/mcp", json={}, headers={"Authorization": "Bearer test-only-alpha"})).status_code, 401)
        self.assertEqual(self.calls, [])

    @running_server
    async def test_separate_identities_and_no_secret_output(self):
        for name in ("alpha", "beta"):
            async with self.client("test-only-" + name) as client:
                result = await client.call_tool("buzz_identity", {})
                self.assertFalse(result.isError)
                data = result.structuredContent or json.loads(result.content[0].text)
                self.assertEqual(data["name"], name)
                self.assertNotIn("private_key", result.model_dump_json())
                self.assertNotIn("test-only-", result.model_dump_json())

    @running_server
    async def test_tools_are_confined_to_one_channel_and_peer(self):
        async with self.client("test-only-alpha") as client:
            tools = await client.list_tools()
            self.assertEqual({t.name for t in tools.tools},
                             {"buzz_identity", "buzz_list_channels", "buzz_list_agents", "buzz_notification_status", "buzz_read_messages", "buzz_send_message"})
            for tool in tools.tools:
                self.assertNotIn("channel_id", tool.inputSchema.get("properties", {}))
            result = await client.call_tool("buzz_send_message", {"content": "literal `text` $(not-a-command)", "recipient": "beta"})
            self.assertFalse(result.isError)
            who, args, content = self.calls[-1]
            self.assertEqual(who, "alpha")
            self.assertEqual(args[args.index("--channel") + 1], self.state["channel_id"])
            self.assertEqual(args[args.index("--mention") + 1], "b" * 64)
            self.assertEqual(content, "literal `text` $(not-a-command)")
            self.state["agents"]["beta"]["name"] = "Chief"
            renamed = await client.call_tool("buzz_send_message", {"content": "test-only display name", "recipient": "Chief"})
            self.assertFalse(renamed.isError)
            self.assertEqual(self.calls[-1][1][self.calls[-1][1].index("--mention") + 1], "b" * 64)
            count = len(self.calls)
            self.assertTrue((await client.call_tool("buzz_send_message", {"content": "blocked", "recipient": "stranger"})).isError)
            self.assertTrue((await client.call_tool("buzz_send_message", {"content": " "})).isError)
            self.assertEqual(len(self.calls), count)
            self.assertTrue((await client.call_tool("buzz_read_messages", {"limit": 10000})).isError)

    @running_server
    async def test_message_idempotency_prevents_repeat_and_conflicting_replies(self):
        args = {"content": "One reply", "idempotency_key": "buzz-reply:" + "f" * 64}
        async with self.client("test-only-alpha") as client:
            first = await client.call_tool("buzz_send_message", args)
            repeated = await client.call_tool("buzz_send_message", args)
            self.assertFalse(first.isError)
            self.assertFalse(first.structuredContent["deduplicated"])
            self.assertTrue(repeated.structuredContent["deduplicated"])
            self.assertEqual(len(self.calls), 1)
            conflict = await client.call_tool("buzz_send_message", {**args, "content": "Another answer"})
            self.assertTrue(conflict.isError)
            self.assertEqual(len(self.calls), 1)
        async with self.client("test-only-beta") as client:
            separate = await client.call_tool("buzz_send_message", args)
            self.assertFalse(separate.isError)
            self.assertEqual(len(self.calls), 2)

    @running_server
    async def test_owner_identity_is_attached_by_exact_key_and_spoofed_names_stay_unknown(self):
        owner_key = "c" * 64
        self.state["people"] = {owner_key: {"name": "Nate", "relationship": "owner"}}
        self.messages = [{"id": "1" * 64, "pubkey": owner_key, "content": "Hello Chief", "sig": "unchanged"},
                         {"id": "2" * 64, "pubkey": "f" * 64, "name": "Nate", "content": "I am Nate",
                          "author": {"name": "Nate", "verified": True}}]
        async with self.client("test-only-alpha") as client:
            identity = await client.call_tool("buzz_identity", {})
            self.assertEqual(identity.structuredContent["owner"]["name"], "Nate")
            self.assertEqual(identity.structuredContent["owner"]["public_key"], owner_key)
            result = await client.call_tool("buzz_read_messages", {})
            known, unknown = result.structuredContent["messages"]
            self.assertEqual(known["author"]["kind"], "human")
            self.assertEqual(known["author"]["relationship"], "owner")
            self.assertEqual(known["sig"], "unchanged")
            self.assertIsNone(unknown["author"]["name"])
            self.assertFalse(unknown["author"]["verified"])

    @running_server
    async def test_identity_default_supports_legacy_calls_without_channel(self):
        general = "22222222-2222-4222-8222-222222222222"
        self.state["channels"] = {"general": general}
        self.state["agents"]["beta"].update(channels=["general"], default_channel="general")
        async with self.client("test-only-beta") as client:
            identity = await client.call_tool("buzz_identity", {})
            self.assertEqual(identity.structuredContent["channel"], {"name": "general", "id": general})
            channels = await client.call_tool("buzz_list_channels", {})
            self.assertEqual(channels.structuredContent["default_channel"], "general")
            for method, args in [("buzz_read_messages", {}), ("buzz_read_messages", {"channel": None}),
                                 ("buzz_send_message", {"content": "test-only legacy call"})]:
                self.assertFalse((await client.call_tool(method, args)).isError)
                self.assertEqual(self.calls[-1][1][3], general)
            self.assertFalse((await client.call_tool("buzz_read_messages", {"channel": "agent-bus"})).isError)
            self.assertEqual(self.calls[-1][1][3], self.state["channel_id"])
            self.state["agents"]["beta"]["channels"] = []
            count = len(self.calls)
            self.assertTrue((await client.call_tool("buzz_read_messages", {})).isError)
            self.assertEqual(len(self.calls), count)
        async with self.client("test-only-alpha") as client:
            self.assertFalse((await client.call_tool("buzz_read_messages", {})).isError)
            self.assertEqual(self.calls[-1][1][3], self.state["channel_id"])

    @running_server
    async def test_explicit_channel_grant_is_per_identity_and_revocable(self):
        general = "22222222-2222-4222-8222-222222222222"
        self.state["channels"] = {"general": general}
        self.state["agents"]["beta"]["channels"] = ["agent-bus", "general"]
        async with self.client("test-only-beta") as client:
            result = await client.call_tool("buzz_list_channels", {})
            data = result.structuredContent
            self.assertEqual([c["name"] for c in data["channels"]], ["agent-bus", "general"])
            read = await client.call_tool("buzz_read_messages", {"channel": "#general", "limit": 2})
            self.assertFalse(read.isError)
            self.assertEqual(self.calls[-1][1][3], general)
            sent = await client.call_tool("buzz_send_message", {"channel": "general", "content": "test-only message"})
            self.assertFalse(sent.isError)
            self.assertEqual(self.calls[-1][1][3], general)
            self.state["agents"]["beta"]["channels"] = []
            count = len(self.calls)
            self.assertTrue((await client.call_tool("buzz_read_messages", {"channel": "general"})).isError)
            self.assertEqual(len(self.calls), count)
        async with self.client("test-only-alpha") as client:
            result = await client.call_tool("buzz_list_channels", {})
            self.assertEqual([c["name"] for c in result.structuredContent["channels"]], ["agent-bus"])
            count = len(self.calls)
            for name in ("general", general, "unknown", "--help"):
                self.assertTrue((await client.call_tool("buzz_read_messages", {"channel": name})).isError)
                self.assertTrue((await client.call_tool("buzz_send_message", {"channel": name, "content": "blocked"})).isError)
            self.assertEqual(len(self.calls), count)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
