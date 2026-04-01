# OpenClaw Internal Hotfix Note

Date applied: 2026-04-02

This is a local runtime hotfix applied directly to the installed OpenClaw bundle to restore token usage tracking for vLLM/OpenAI-style responses.

## What was patched

Installed file patched:

- `/home/kanthai/.npm-global/lib/node_modules/openclaw/dist/pi-embedded-iRgRpYxO.js`

Patched area (around line ~33139):

- Before: usage mapping only read `input_tokens` / `output_tokens` / `total_tokens`.
- After: usage mapping falls back to `prompt_tokens` / `completion_tokens` when needed, then computes `totalTokens` if missing.

Effective logic:

```js
const responseUsage = response.usage;
const inputTokens = responseUsage?.input_tokens ?? responseUsage?.prompt_tokens ?? 0;
const outputTokens = responseUsage?.output_tokens ?? responseUsage?.completion_tokens ?? 0;
const totalTokens = responseUsage?.total_tokens ?? inputTokens + outputTokens;
```

## Why this was needed

vLLM (OpenAI-compatible responses) may return usage as:

- `prompt_tokens`
- `completion_tokens`
- `total_tokens`

OpenClaw in this build path expected `input_tokens` / `output_tokens`, causing usage to persist as zero and Clawmetry to show no token usage.

## Runtime action taken

Gateway restarted after patch:

```bash
systemctl --user restart openclaw-gateway.service
```

## Durability warning

This is a patch to built `dist` output in a global npm install, not source-level code.
Any OpenClaw update/reinstall can overwrite it.
Keep a source-level fix in your own overlay/proxy layer (already added in `vllm-think-proxy.py`) as the durable path.
