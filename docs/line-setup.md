# LINE Messaging API Setup

## Overview

LINE is a native OpenClaw channel via the bundled `@openclaw/line` plugin.
The webhook is exposed publicly using Tailscale Funnel (HTTPS, no port forwarding needed).

Webhook URL: `https://<your-ts-hostname>/line/webhook`

---

## Installation

```bash
openclaw plugins install @openclaw/line
```

The plugin is bundled with OpenClaw — no npm package needed. It installs from
`~/.npm-global/lib/node_modules/openclaw/dist/extensions/line`.

---

## Configuration (openclaw.json)

```json
"channels": {
  "line": {
    "enabled": true,
    "channelAccessToken": "<long-lived token from LINE Developers Console>",
    "channelSecret": "<channel secret>",
    "dmPolicy": "pairing"
  }
},
"gateway": {
  "auth": {
    "mode": "password",
    "password": {
      "source": "env",
      "provider": "env",
      "id": "OPENCLAW_GATEWAY_PASSWORD"
    }
  },
  "tailscale": {
    "mode": "funnel",
    "resetOnExit": false
  }
}
```

**Note:** Tailscale Funnel requires `gateway.auth.mode = "password"` — the default
`"token"` mode is rejected with:
> `tailscale funnel requires gateway auth mode=password`

Add `OPENCLAW_GATEWAY_PASSWORD` to both `~/.secrets` (for the systemd service)
and `secrets.providers.env.allowlist` in openclaw.json.

---

## Tailscale Funnel Setup

Tailscale Funnel exposes the OpenClaw gateway (port 18789) to the public internet via HTTPS.

### One-time prerequisites

1. Enable Funnel on your tailnet:
   Visit `https://login.tailscale.com/f/funnel?node=<your-node-id>`

2. Allow your user to configure Tailscale without sudo:
   ```bash
   sudo tailscale set --operator=$USER
   ```

### Start persistent background funnel

```bash
tailscale funnel --bg --yes 18789
```

This is idempotent — safe to run again if already active. Survives reboots.

OpenClaw also attempts to configure Funnel on startup via `gateway.tailscale.mode: "funnel"`.
With the operator permission set, this succeeds automatically. Running manually first is a safe
workaround if OpenClaw starts before the permission is set.

### Verify public access

```bash
# What public DNS sees (should return Tailscale relay IPs, not 100.x.x.x):
dig @8.8.8.8 <your-ts-hostname> +short

# Test via relay IP:
curl --resolve "<hostname>:443:<relay-ip>" https://<hostname>/line/webhook
```

---

## LINE Developers Console

1. Create a Messaging API channel at https://developers.line.biz/console/
2. Under **Messaging API** tab:
   - **Webhook URL**: `https://<your-ts-hostname>/line/webhook`
   - Click **Verify** — should return 200
   - Enable **"Use webhook"** toggle
3. Disable **"Auto-reply messages"** (LINE's default auto-reply conflicts with Nyx)

---

## User Pairing

`dmPolicy: "pairing"` means users must be approved before chatting.
When a new user messages the bot, OpenClaw replies with a pairing code.

Approve with:
```bash
openclaw pairing approve line <CODE>
```

---

## Diagnostics

### Verify failed in LINE console

Check if the request hit the server:
```bash
journalctl --user -u openclaw-gateway.service -f --no-pager
# Then click Verify in LINE console
```

If no log entry appears → public connectivity issue (Funnel not running or not yet active).
If a log entry appears with a non-200 response → signature mismatch or wrong channel secret.

### Funnel not working publicly

Test via public relay IP (bypasses local Tailscale MagicDNS):
```bash
RELAY=$(dig @8.8.8.8 <hostname> +short | head -1)
curl --resolve "<hostname>:443:$RELAY" https://<hostname>/line/webhook
```

If this fails but internal curl works, the Funnel background daemon isn't running:
```bash
tailscale funnel --bg --yes 18789
```

### OpenClaw logs `[tailscale] funnel failed` on startup

Operator permission not set. Run once:
```bash
sudo tailscale set --operator=$USER
```

### `ws: origin not allowed` in logs

This is the OpenClaw Control UI (browser) being blocked via the Tailscale domain —
not related to LINE. Normal behavior when accessing the UI from outside localhost.
