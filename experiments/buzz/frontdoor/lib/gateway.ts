// Transparent HTTP front door for the existing local MCP server.
// Independent of the Sites /mcp plugin endpoint and its authentication.
export interface RouteRecord {
  upstream: string;
  token_hashes: string;
  updated_at: string;
}

interface RouteDatabase {
  prepare(query: string): {
    bind(...values: unknown[]): { run(): Promise<unknown> };
    first<T>(): Promise<T | null>;
  };
}

export interface GatewayEnv {
  DB?: RouteDatabase;
  BUZZ_ROUTING_KEY?: string;
}

const MAX_BODY = 64 * 1024;
const JSON_HEADERS = { "Content-Type": "application/json", "Cache-Control": "no-store" };

function json(value: unknown, status = 200, extra: Record<string, string> = {}) {
  return new Response(JSON.stringify(value), { status, headers: { ...JSON_HEADERS, ...extra } });
}

export function validUpstream(value: unknown): value is string {
  return typeof value === "string" && /^https:\/\/[a-z0-9-]+\.trycloudflare\.com\/mcp$/.test(value);
}

function bearer(request: Request): string | null {
  const match = /^Bearer ([A-Za-z0-9_-]{20,256})$/i.exec(request.headers.get("Authorization") ?? "");
  return match?.[1] ?? null;
}

async function digest(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), (n) => n.toString(16).padStart(2, "0")).join("");
}

function equalHash(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let different = 0;
  for (let index = 0; index < left.length; index++) different |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return different === 0;
}

export async function bodyBytes(request: Request): Promise<Uint8Array> {
  const declared = Number(request.headers.get("Content-Length") ?? 0);
  if (declared > MAX_BODY) throw new RangeError("Request too large");
  const reader = request.body?.getReader();
  if (!reader) return new Uint8Array();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > MAX_BODY) {
      await reader.cancel();
      throw new RangeError("Request too large");
    }
    chunks.push(value);
  }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}

async function route(env: GatewayEnv): Promise<RouteRecord | null> {
  if (!env.DB) return null;
  return env.DB.prepare("SELECT upstream, token_hashes, updated_at FROM gateway_routes WHERE id = 'default'").first<RouteRecord>();
}

function unauthorized() {
  return json({ error: "invalid_token", error_description: "An agent bearer credential is required" }, 401,
    { "WWW-Authenticate": "Bearer" });
}

export async function gatewayRequest(request: Request, env: GatewayEnv, fetcher: typeof fetch = fetch): Promise<Response | null> {
  const path = new URL(request.url).pathname;
  if (!["/api/mcp", "/internal/register", "/health"].includes(path)) return null;

  try {
    if (path === "/internal/register") {
      if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405, { Allow: "POST" });
      const token = bearer(request);
      if (!env.BUZZ_ROUTING_KEY) return json({ error: "routing_unavailable" }, 503);
      if (!token || !equalHash(await digest(token), await digest(env.BUZZ_ROUTING_KEY))) return unauthorized();
      if (!env.DB) return json({ error: "routing_unavailable" }, 503);
      const data = JSON.parse(new TextDecoder().decode(await bodyBytes(request)));
      if (!validUpstream(data.upstream) || !Array.isArray(data.token_hashes) || data.token_hashes.length > 100 ||
          !data.token_hashes.every((hash: unknown) => typeof hash === "string" && /^[a-f0-9]{64}$/.test(hash))) {
        return json({ error: "invalid_registration" }, 400);
      }
      const now = new Date().toISOString();
      await env.DB.prepare(
        "INSERT INTO gateway_routes (id, upstream, token_hashes, updated_at) VALUES (?, ?, ?, ?) " +
        "ON CONFLICT(id) DO UPDATE SET upstream=excluded.upstream, token_hashes=excluded.token_hashes, updated_at=excluded.updated_at"
      ).bind("default", data.upstream, JSON.stringify([...new Set(data.token_hashes)]), now).run();
      return json({ registered: true, updated_at: now });
    }

    if (path === "/health") {
      if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405, { Allow: "GET" });
      const current = await route(env);
      if (!current || !validUpstream(current.upstream)) return json({ online: false }, 503);
      const health = new URL("/health", current.upstream);
      try {
        const response = await fetcher(health, { redirect: "manual", signal: AbortSignal.timeout(5000) });
        const healthy = response.ok && (await response.json() as { status?: string }).status === "ready";
        return json({ online: healthy }, healthy ? 200 : 503);
      } catch { return json({ online: false }, 503); }
    }

    const token = bearer(request);
    if (!token) return unauthorized();
    const current = await route(env);
    if (!current || !validUpstream(current.upstream)) return json({ error: "local_gateway_unavailable" }, 503);
    const hashes: string[] = JSON.parse(current.token_hashes);
    const supplied = await digest(token);
    if (!hashes.some((hash) => equalHash(supplied, hash))) return unauthorized();
    if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405, { Allow: "POST" });
    if (!(request.headers.get("Content-Type") ?? "").toLowerCase().startsWith("application/json")) {
      return json({ error: "json_required" }, 400);
    }
    const body = await bodyBytes(request);
    const headers = new Headers({ "Content-Type": "application/json", Accept: "application/json, text/event-stream",
      Authorization: "Bearer " + token });
    const version = request.headers.get("MCP-Protocol-Version");
    if (version) headers.set("MCP-Protocol-Version", version);
    let upstream: Response;
    try {
      upstream = await fetcher(current.upstream, { method: "POST", headers, body: body as BodyInit,
        redirect: "manual", signal: AbortSignal.timeout(25000) });
    } catch { return json({ error: "local_gateway_unavailable" }, 503); }
    if (upstream.status >= 300 && upstream.status < 400) return json({ error: "upstream_redirect_refused" }, 502);
    const outgoing = new Headers({ "Cache-Control": "no-store" });
    for (const key of ["Content-Type", "WWW-Authenticate", "Retry-After"]) {
      const value = upstream.headers.get(key);
      if (value) outgoing.set(key, value);
    }
    return new Response(upstream.body, { status: upstream.status, headers: outgoing });
  } catch (error) {
    if (error instanceof RangeError) return json({ error: "request_too_large" }, 413);
    if (error instanceof SyntaxError) return json({ error: "invalid_json" }, 400);
    return json({ error: "gateway_unavailable" }, 503);
  }
}
