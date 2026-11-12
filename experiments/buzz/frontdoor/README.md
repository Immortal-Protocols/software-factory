# Buzz hosted front door

This is the Sites/Vinext Worker source for the experiment's stable HTTPS gateway. It includes the starter's build helpers and vendored licenses. The public source removes the original deployment ID and derives the displayed MCP URL from the current origin.

| Route | Behavior |
| :--- | :--- |
| `/api/mcp` | Authenticate the caller's per-agent bearer token and proxy JSON Streamable HTTP requests to the registered tunnel. |
| `/internal/register` | With the separate routing secret, replace the permitted tunnel origin and enabled token hashes. |
| `/mcp` | Discover Chief's tools/events and forward owner-authorized calls using Chief's dedicated agent token. |
| `/health` | Return a coarse online/offline result without exposing the tunnel address or credentials. |

`lib/gateway.ts` permits only HTTPS Quick Tunnel upstreams with the exact `/mcp` path. It refuses redirects, bounds request bodies and timeouts, and forwards only selected headers. D1 holds the current route and token hashes. `lib/personal-mcp.ts` requires Sites-attested user identity headers and the configured owner's email hash before forwarding tools or event subscriptions.

The `/mcp` owner check relies on the hosting platform to supply verified identity headers. Do not serve it behind a generic public reverse proxy that accepts user-supplied copies of those headers.

## Private deployment configuration

Provision a Sites project with the `DB` D1 binding and MCP capability declared in `.openai/hosting.json`. Apply the included `drizzle/` migration through the hosting workflow. Provision these runtime secrets outside Git:

- `BUZZ_ROUTING_KEY`: registration credential shared with the local private `.routing.json`.
- `DOT_AGENT_TOKEN`: Chief's dedicated local gateway bearer credential.
- `DOT_OWNER_EMAIL_HASH`: SHA-256 of the intended owner's trimmed, lowercased account email.

Register the current tunnel with `control.py start` on the local host. Register the native `/mcp` plugin through the authenticated hosting platform. Agent clients use `/api/mcp` and their own bearer credentials. Deployment is intentionally separate from committing source; this snapshot does not redeploy or connect to the original running site.

## Checks

The gateway tests use Node's built-in runner and synthetic state; they do not need installed dependencies:

```sh
npm test
```

For a full build, use Node.js 22.13 or newer and install locked dependencies:

```sh
npm ci
npm run lint
npm run build
```

When using the Sites plugin, follow its installation/build/publish workflow. The included `.sites-runtime` and `.wrangler` state directories are disposable, generated locally, and ignored by Git.
