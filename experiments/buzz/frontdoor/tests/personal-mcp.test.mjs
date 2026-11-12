import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { personalMcpRequest } from "../lib/personal-mcp.ts";

const email = "owner@test.example";
const dotToken = "buzz_test_dot_012345678901234567890123456789";
const hash = value => createHash("sha256").update(value).digest("hex");
const request = (method, params = {}, headers = {}) => new Request("https://gateway.example/mcp", {
  method: "POST", headers: { "Content-Type": "application/json", ...headers },
  body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
});

function env() {
  return { DOT_AGENT_TOKEN: dotToken, DOT_OWNER_EMAIL_HASH: hash(email), DB: {
    prepare: () => ({ first: async () => ({ upstream: "https://test-origin.trycloudflare.com/mcp",
      token_hashes: JSON.stringify([hash(dotToken)]), updated_at: "now" }) }),
  } };
}

test("discovery and schemas work offline without exposing credentials", async () => {
  const discovery = await personalMcpRequest(request("server/discover"), {});
  const discovered = (await discovery.json()).result;
  assert.deepEqual(discovered.supportedVersions, ["2026-07-28"]);
  assert.deepEqual(discovered.capabilities.events, {});
  const list = await personalMcpRequest(request("tools/list"), env());
  const body = await list.text();
  const tools = JSON.parse(body).result.tools;
  assert.deepEqual(tools.map(t => t.name).sort(), ["buzz_identity", "buzz_list_agents", "buzz_list_channels", "buzz_notification_status", "buzz_read_messages", "buzz_send_message"]);
  for (const name of ["buzz_read_messages", "buzz_send_message"]) {
    assert.equal(tools.find(t => t.name === name).inputSchema.properties.channel.default, null);
  }
  assert.equal(tools.find(t => t.name === "buzz_send_message").annotations.readOnlyHint, false);
  assert.equal(body.includes(dotToken), false);
  assert.equal(body.includes(email), false);
  const eventList = await personalMcpRequest(request("events/list"), env());
  const definitions = (await eventList.json()).result.events;
  assert.equal(definitions[0].name, "buzz.message.created");
  assert.deepEqual(definitions[0].delivery, ["webhook"]);
  assert.equal(definitions[0].payloadSchema.properties.author.properties.kind.enum.includes("human"), true);
  // Existing queued notifications remain valid while new ones add author data.
  assert.equal(definitions[0].payloadSchema.required.includes("author"), false);
});

test("event subscriptions enforce owner identity and forward the callback only to local Buzz", async () => {
  const params = { name: "buzz.message.created", arguments: { channel: "general", mentions_only: true },
    delivery: { mode: "webhook", url: "https://callbacks.example/test", secret: "whsec_test-only" } };
  let forwarded;
  const fetcher = async (url, init) => {
    forwarded = JSON.parse(new TextDecoder().decode(init.body));
    return Response.json({ jsonrpc: "2.0", id: 1, result: { id: "test-subscription" } });
  };
  const anonymous = await personalMcpRequest(request("events/subscribe", params), env(), fetcher);
  assert.equal(anonymous.status, 403);
  assert.equal(forwarded, undefined);
  const headers = { "oai-authenticated-user-id": "owner-site-id", "oai-authenticated-user-email": email };
  const subscribed = await personalMcpRequest(request("events/subscribe", params, headers), env(), fetcher);
  assert.equal((await subscribed.json()).result.id, "test-subscription");
  assert.equal(forwarded.method, "events/subscribe");
  assert.deepEqual(forwarded.params, params);
  const unavailable = await personalMcpRequest(request("events/unsubscribe", params, headers), env(), async () => new Response(null, { status: 503 }));
  assert.equal((await unavailable.json()).error.code, -32603);
});

test("tool calls reject missing or other account identities before reaching Buzz", async () => {
  let forwards = 0;
  const fetcher = async () => { forwards++; return Response.json({}); };
  for (const headers of [{}, { "oai-authenticated-user-id": "other", "oai-authenticated-user-email": "other@test.example" },
    { "oai-authenticated-user-email": email }]) {
    const result = await personalMcpRequest(request("tools/call", { name: "buzz_read_messages", arguments: {} }, headers), env(), fetcher);
    assert.equal(result.status, 403);
  }
  assert.equal(forwards, 0);
});

test("the attested owner uses only the dedicated Dot token and the existing scoped gateway", async () => {
  const headers = { "oai-authenticated-user-id": "owner-site-id", "oai-authenticated-user-email": email };
  let forwarded;
  const fetcher = async (url, init) => { forwarded = { url, init }; return Response.json({ jsonrpc: "2.0", id: 1, result: { content: [] } }); };
  const result = await personalMcpRequest(request("tools/call", { name: "buzz_read_messages", arguments: { channel: "general", limit: 3 } }, headers), env(), fetcher);
  assert.equal(result.status, 200);
  assert.equal(forwarded.url, "https://test-origin.trycloudflare.com/mcp");
  assert.equal(forwarded.init.headers.get("Authorization"), "Bearer " + dotToken);
  assert.equal(forwarded.init.headers.get("MCP-Protocol-Version"), "2025-11-25");
  assert.equal(forwarded.init.headers.get("oai-authenticated-user-email"), null);
  assert.equal(JSON.parse(new TextDecoder().decode(forwarded.init.body)).params.arguments.channel, "general");
  const bad = await personalMcpRequest(request("tools/call", { name: "shell", arguments: {} }, headers), env(), fetcher);
  assert.equal((await bad.json()).result.isError, true);
});
