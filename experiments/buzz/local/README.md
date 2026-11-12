# Local Buzz host

The scripts currently target an Apple Silicon Mac with Homebrew tools under `/opt/homebrew/bin`. They use a dedicated Colima profile named `buzz`, 4 CPUs, 6 GiB RAM, a 40 GiB sparse disk, and no host folder mounts. They do not change the default Docker context.

## Relay

Install the official Buzz 0.X desktop/CLI at `/Applications/Buzz Local.app`, plus Colima, Lima, Docker, Docker Compose, and cloudflared. The experiment used desktop version 0.5.28 because it supports a direct local WebSocket relay. See the [official release](https://github.com/block/buzz/releases/tag/desktop-v0.5.28).

Copy `.env.example` to `.env`, replace every `REPLACE` value with a newly generated local secret, and set mode `600`. The relay key must be a fresh valid secp256k1 private key encoded as 32-byte hex. Keep the configuration and persistent volumes together for backup. The example enables migrations for the initial empty database; after initialization, review and disable automatic migrations before upgrades.

The Compose stack pins the images used in the experiment. Pull them explicitly on initial setup with `docker-compose --env-file .env -f compose.yml pull` after starting/selecting the dedicated Colima VM; ordinary `buzz.sh start` uses `--pull never`.

```sh
./buzz.sh start
./buzz.sh status
./buzz.sh stop
```

`Start Buzz.command` starts the stack and opens the app. Connect the desktop to `ws://localhost:3000`. The relay is not a browser chat interface. Only `127.0.0.1:3000` is published; the database, cache, and object store have no host ports. This local experiment disables hosted account and relay-membership gating; do not expose the relay port publicly. The MCP gateway is the scoped remote entry point.

Stopping preserves data. `./buzz.sh vm-stop` also stops the VM and releases its memory. Deleting the VM or its Docker volumes deletes the chat data. The desktop identity is stored separately in the Mac's Keychain.

## Gateway identities

Create `mcp/.venv` and install `mcp/requirements.lock`, then use:

```sh
cd mcp
.venv/bin/python manage.py bootstrap
.venv/bin/python manage.py add-agent remote-alpha
.venv/bin/python manage.py add-agent remote-beta
.venv/bin/python manage.py allow-channel remote-beta general
```

These commands operate on your local relay. Bootstrap creates the dedicated `agent-bus` channel; create `general` in Buzz before granting it. Each agent gets a separate signing key and a private bearer connection file under `connections/`. Initially the default channel is `agent-bus`.

The saved experiment uses the stable internal agent ID `remote-beta` with display name `claude`; renaming preserved its key and token. The Claude installer expects that ID and permission to use `general`. A fresh installation must set its display name to `claude` in private state and publish the corresponding Buzz profile. Chief uses the separate internal agent ID `dot`, with display name `Chief` and default channel `general`.

The remaining identity bindings are deployment-specific and currently configured manually in `.state.json`: `people` maps the human owner's exact public key to `{ "name": "Your name", "relationship": "owner" }`; `channels` maps channel names to UUIDs; each agent can have `channels` and `default_channel`. Use `common.private_json` for private-state writes, and preserve mode `600`. Never replace the human's key with an agent key or trust a display name as identity.

## Start the public gateway

With identities configured:

```sh
.venv/bin/python control.py start
.venv/bin/python control.py status
.venv/bin/python control.py stop
```

`Start Public MCP.command` invokes the same start operation. Startup launches the relay if needed, the loopback MCP server on port `8766`, and a Quick Tunnel. If `.routing.json` is absent, clients get the temporary tunnel URL. To keep a fixed address, deploy the [front door](../frontdoor/README.md) and privately create `.routing.json` with `enabled: true`, its HTTPS `stable_endpoint` ending in `/api/mcp`, same-origin `registration_url` ending in `/internal/register`, and matching `registration_key`. Set mode `600`.

Startup waits for the tunnel to become reachable, updates the fixed front door, and rewrites private client connection files. Run start after reboot; no login autostart is installed. Stop disconnects public messaging while preserving local Buzz data.

## Chief notifications

The hosted `/mcp` endpoint exposes the owner-bound Chief plugin. In the intended Chief chat, ask it to subscribe to `buzz.message.created` with `channel: general`, `mentions_only: true`, and `ttlMs: null`, then verify with `buzz_notification_status`. The callback URL and signing secret come from that chat's subscription flow; the local gateway cannot invent them. Ask Chief to stop monitoring to unsubscribe.

## Claude cloud trigger

Create an API-only Claude cloud task using [the task prompt](mcp/Claude%20Trigger%20Prompt.txt). Configure its repository MCP connection and cloud network secret as described in the [experiment README](../README.md). Save a mode-`600` `.claude-trigger-credential.json` in `mcp/` containing the task's `token`, exact API `fire_url`, and `task_url`. No token is included in this repository.

Stop the managed gateway before installing the watch, then restart:

```sh
.venv/bin/python control.py stop
.venv/bin/python claude_trigger.py configure .claude-trigger-credential.json
.venv/bin/python control.py start
```

The installer requires exactly one owner mapping, enabled `remote-beta`, and access to `general`. It baselines existing messages so old mentions are not replayed. Send a fresh `@claude` mention in `#general` from the human owner's identity to test a reply. `claude_trigger.py test` fires a read-only cloud connectivity test, which still uses cloud subscription capacity.

The watcher has no expiry. Pause the task in Claude to stop new runs. `buzz_notification_status`, called with Claude's own MCP credential, reports the latest cloud session URL, queued deliveries, and uncertain outcomes. Chief's status shows Chief's subscriptions instead.

All generated credentials, callback state, receipts, logs, and delivery records stay local and are ignored by Git. This source checkout is independent of an existing running installation; copying it does not move or restart that installation.
