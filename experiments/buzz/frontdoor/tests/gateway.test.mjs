import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { gatewayRequest } from "../lib/gateway.ts";

const origin = "https://test-origin.trycloudflare.com/mcp";
const agent = "buzz_test_agent_012345678901234567890123456789";
const routingKey = "routing_test_012345678901234567890123456789";
const hash = value => createHash("sha256").update(value).digest("hex");

function database(initial = null) {
  let row = initial;
  return {
    get row() { return row; },
    prepare() {
      return {
        first: async () => row,
        bind: (id, upstream, hashes, updated) => ({ run: async () => { row = { upstream, token_hashes: hashes, updated_at: updated }; } }),
      };
    },
  };
}

const request = (path, token, body = {}, extra = {}) => new Request("https://gateway.example" + path, {
  method: "POST", headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}), ...extra },
  body: typeof body === "string" ? body : JSON.stringify(body),
});

test("only the routing credential can register an allowed upstream", async () => {
  const DB = database();
  const env = { DB, BUZZ_ROUTING_KEY: routingKey };
  const data = { upstream: origin, token_hashes: [hash(agent)] };
  assert.equal((await gatewayRequest(request("/internal/register", agent, data), env)).status, 401);
  assert.equal(DB.row, null);
  for (const upstream of ["http://127.0.0.1/mcp", "https://example.com/mcp", "https://a.trycloudflare.com@evil.example/mcp", origin + "?target=bad"]) {
    assert.equal((await gatewayRequest(request("/internal/register", routingKey, { ...data, upstream }), env)).status, 400);
  }
  assert.equal((await gatewayRequest(request("/internal/register", routingKey, data), env)).status, 200);
  assert.equal(DB.row.upstream, origin);
  assert.deepEqual(JSON.parse(DB.row.token_hashes), [hash(agent)]);
});

test("agent tokens protect the fixed endpoint and only selected headers are forwarded", async () => {
  const DB = database({ upstream: origin, token_hashes: JSON.stringify([hash(agent)]), updated_at: "now" });
  const calls = [];
  const forward = async (url, init) => { calls.push({ url, init }); return Response.json({ result: { ok: true } }); };
  for (const token of [null, routingKey, "invalid_012345678901234567890123456789"]) {
    assert.equal((await gatewayRequest(request("/api/mcp", token), { DB }, forward)).status, 401);
  }
  assert.equal(calls.length, 0);
  const result = await gatewayRequest(request("/api/mcp", agent, { jsonrpc: "2.0", id: 1, method: "tools/list" },
    { Cookie: "do-not-forward=secret", "MCP-Protocol-Version": "2025-11-25" }), { DB }, forward);
  assert.equal(result.status, 200);
  assert.equal(calls[0].url, origin);
  assert.equal(calls[0].init.headers.get("Authorization"), "Bearer " + agent);
  assert.equal(calls[0].init.headers.get("Cookie"), null);
  assert.equal(calls[0].init.headers.get("MCP-Protocol-Version"), "2025-11-25");
  assert.equal(calls[0].init.redirect, "manual");
});

test("routing changes preserve the public path; oversized requests and redirects are refused", async () => {
  const DB = database({ upstream: origin, token_hashes: JSON.stringify([hash(agent)]), updated_at: "now" });
  const env = { DB, BUZZ_ROUTING_KEY: routingKey };
  assert.equal((await gatewayRequest(request("/api/mcp", agent, "x".repeat(65537)), env)).status, 413);
  const next = "https://second-origin.trycloudflare.com/mcp";
  await gatewayRequest(request("/internal/register", routingKey, { upstream: next, token_hashes: [hash(agent)] }), env);
  let target;
  await gatewayRequest(request("/api/mcp", agent), env, async url => { target = url; return Response.json({ ok: true }); });
  assert.equal(target, next);
  const result = await gatewayRequest(request("/api/mcp", agent), env,
    async () => new Response(null, { status: 302, headers: { Location: "https://evil.example" } }));
  assert.equal(result.status, 502);
  assert.equal(result.headers.get("Location"), null);
});
