"""Claude trigger filters, fire semantics, durable queues, and reply deduplication."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from buzz_events import EventService
from claude_trigger import WATCH_ID, fire_request, session_from_response, validate_credential
from message_receipts import MessageReceipts


class ClaudeTriggerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.json"
        self.now = 1794440000.0
        self.state = {"channel_id": "bus", "channels": {"general": "general-id"},
            "people": {"b" * 64: {"name": "Nate", "relationship": "owner"}},
            "agents": {"remote-beta": {"name": "claude", "public_key": "a" * 64,
                "enabled": True, "token_hash": "test-hash", "channels": ["general"]}}}
        self.rows, self.sent, self.effects = [], [], []

        async def runner(agent, args):
            channel = args[args.index("--channel") + 1]
            since, before = (int(args[args.index(flag) + 1]) for flag in ("--since", "--before"))
            limit = int(args[args.index("--limit") + 1])
            return [copy.deepcopy(r) for r in self.rows if r["channel_id"] == channel and since <= r["created_at"] <= before][:limit]

        async def sender(url, body, headers):
            self.sent.append((url, json.loads(body), headers.copy()))
            if self.effects:
                effect = self.effects.pop(0)
                if isinstance(effect, BaseException):
                    raise effect
                return effect
            return 200, json.dumps({"type": "routine_fire", "claude_code_session_url":
                f"https://claude.ai/code/session_test{len(self.sent)}"}).encode(), {}

        self.runner, self.sender = runner, sender
        self.service = self.new_service()
        self.service.data["subscriptions"][WATCH_ID] = {"id": WATCH_ID, "delivery_type": "claude_routine",
            "agent_id": "remote-beta", "token_hash": "test-hash", "channel": "general", "mentions_only": True,
            "mention_text": "claude", "author_public_key": "b" * 64, "expires_at": None, "active": True,
            "started_at": int(self.now), "scan_from": int(self.now), "seen": [], "pending": {},
            "url": "https://api.anthropic.com/v1/claude_code/routines/trig_Test123/fire",
            "routine_token": "sk-ant-test-only-not-a-real-token-1234567890"}
        self.service.save()

    def new_service(self):
        return EventService(lambda: copy.deepcopy(self.state), self.runner, self.path,
                            clock=lambda: self.now, routine_sender=self.sender)

    def message(self, number, author="b", content="hello", mentioned=True, channel="general-id"):
        self.rows.append({"id": f"{number:064x}", "pubkey": author * 64, "created_at": int(self.now),
            "channel_id": channel, "content": content, "tags": [["p", "a" * 64]] if mentioned else []})

    async def test_only_owner_mentions_in_general_fire_and_duplicate_scans_do_not_refire(self):
        self.now += 2
        self.message(1)
        self.message(2, author="c")
        self.message(3, author="a")
        self.message(4, channel="bus")
        self.message(5, mentioned=False)
        self.message(6, content="@claude can you answer?", mentioned=False)
        self.message(7, content="@claudette hello", mentioned=False)
        await self.service.tick()
        await self.service.tick()
        self.assertEqual(len(self.sent), 2)
        context = json.loads(self.sent[0][1]["text"])
        self.assertEqual(context["source"], "buzz")
        self.assertEqual(context["event"]["data"]["author"]["name"], "Nate")
        self.assertEqual(self.sent[0][2]["anthropic-version"], "2026-10-21")
        status = self.service.status("remote-beta")
        self.assertEqual(status["subscriptions"][0]["last_session_url"], "https://claude.ai/code/session_test2")
        self.assertNotIn("sk-ant-", json.dumps(status))
        self.assertNotIn("routine_token", json.dumps(status))
        restarted = self.new_service()
        await restarted.tick()
        self.assertEqual(len(self.sent), 2)

    async def test_ambiguous_network_outcome_is_not_retried_after_restart(self):
        self.now += 2
        self.message(10)
        self.effects = [TimeoutError()]
        await self.service.tick()
        self.now += 60
        restarted = self.new_service()
        await restarted.tick()
        self.assertEqual(len(self.sent), 1)
        status = restarted.status("remote-beta")["subscriptions"][0]
        self.assertEqual(status["uncertain_deliveries"], 1)
        self.assertEqual(status["pending_deliveries"], 0)

    async def test_process_interruption_during_fire_does_not_create_a_second_session(self):
        self.now += 2
        self.message(11)
        self.effects = [asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.service.tick()
        restarted = self.new_service()
        await restarted.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(restarted.status("remote-beta")["subscriptions"][0]["uncertain_deliveries"], 1)

    async def test_rate_limit_respects_retry_after_and_retries_only_unaccepted_fire(self):
        self.now += 2
        self.message(12)
        self.effects = [(429, b"{}", {"Retry-After": "120"})]
        await self.service.tick()
        self.now += 119
        await self.service.tick()
        self.assertEqual(len(self.sent), 1)
        self.now += 1
        await self.service.tick()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.service.status("remote-beta")["subscriptions"][0]["pending_deliveries"], 0)

    async def test_revocation_stops_fire_and_bad_credentials_disable_watch(self):
        self.now += 2
        self.message(13)
        self.state["agents"]["remote-beta"]["enabled"] = False
        await self.service.tick()
        self.assertEqual(self.sent, [])
        self.state["agents"]["remote-beta"]["enabled"] = True
        self.service.data["subscriptions"][WATCH_ID]["active"] = True
        self.effects = [(401, b"{}", {})]
        await self.service.tick()
        self.assertFalse(self.service.status("remote-beta")["subscriptions"][0]["active"])

    def test_only_exact_claude_fire_endpoint_is_allowed(self):
        token = "sk-ant-test-only-not-a-real-token-1234567890"
        for url in ("https://example.com/fire", "http://api.anthropic.com/v1/claude_code/routines/trig_X/fire",
                    "https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/claude_code/routines/trig_X/fire?redirect=1"):
            with self.assertRaises(ValueError):
                validate_credential({"fire_url": url, "token": token})
        url, body, headers = fire_request(self.service.data["subscriptions"][WATCH_ID], test_only=True)
        self.assertEqual(json.loads(json.loads(body)["text"]), {"source": "buzz", "test_only": True})
        for prefix in ("session", "cse"):
            expected = f"https://claude.ai/code/{prefix}_Test123"
            response = json.dumps({"type": "routine_fire", "claude_code_session_url": expected})
            self.assertEqual(session_from_response(response), expected)


class MessageReceiptTest(unittest.IsolatedAsyncioTestCase):
    async def test_successful_reply_is_deduplicated_across_restart_and_conflicts_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.sqlite3"
            calls = []

            async def send():
                calls.append(1)
                return {"result": {"event_id": "c" * 64}}

            first = MessageReceipts(path)
            await first.send_once("agent", "message1", {"content": "hello"}, send)
            second = MessageReceipts(path)
            result = await second.send_once("agent", "message1", {"content": "hello"}, send)
            self.assertTrue(result["deduplicated"])
            self.assertEqual(len(calls), 1)
            with self.assertRaises(ValueError):
                await second.send_once("agent", "message1", {"content": "different"}, send)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            first.db.close()
            second.db.close()

    async def test_failed_or_interrupted_send_is_not_automatically_posted_again(self):
        receipts = MessageReceipts()
        calls = []

        async def fail():
            calls.append(1)
            raise TimeoutError()

        with self.assertRaises(TimeoutError):
            await receipts.send_once("agent", "message2", {}, fail)
        with self.assertRaises(ValueError):
            await receipts.send_once("agent", "message2", {}, fail)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
