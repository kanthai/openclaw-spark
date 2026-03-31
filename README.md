# openclaw-spark

Reference documentation for running **OpenClaw + vLLM on NVIDIA DGX Spark (GB10)**.

This repo captures the exact configuration, workarounds, and diagnostic notes from a production single-node DGX Spark setup. It is intended as a reference for LLMs (and humans) diagnosing issues with this stack.

---

## Hardware

| Component | Spec |
|-----------|------|
| Machine | NVIDIA DGX Spark (single node) |
| GPU/CPU | GB10 Superchip — Grace CPU + Blackwell GPU, unified memory |
| GPU Architecture | SM121 (Blackwell) |
| Unified Memory | 121 GB (shared between CPU and GPU) |
| Host | `claw-brain` |

**Key implication of unified memory:** KV cache, model weights, and CPU RAM all share the same 121 GB pool. There is no separate VRAM — `gpu_memory_utilization` is a fraction of total unified memory reserved for the vLLM process.

---

## Software Stack

| Layer | Component |
|-------|-----------|
| Container runtime | Docker |
| vLLM container | `eugr/spark-vllm-docker` — custom vllm-node image |
| vLLM build | FlashInfer compiled for SM121, NVFP4 quant fix (PR #38126) |
| Attention backend | FlashInfer |
| Frontend / agent platform | OpenClaw |
| Telegram bot | OpenClaw Telegram channel (Kan's personal instance) |

### spark-vllm-docker

Repo: https://github.com/eugr/spark-vllm-docker

This is the launcher framework. It provides:
- `run-recipe.sh` — wraps `run-recipe.py`, reads YAML recipe files
- `launch-cluster.sh` — underlying Docker launcher
- `mods/` — patches applied at launch (e.g. custom chat templates)
- `recipes/` — YAML files defining model, container, vLLM flags, env vars

**`--solo` flag:** Runs a single slot without launching SlotF alongside. It does NOT mean single-node (the whole setup is already single-node). It means "don't start the companion worker slot."

---

## Slot Layout

All slots serve on `localhost` and are accessed by OpenClaw via `http://127.0.0.1:<port>/v1`.

| Slot | Port | Model | Notes |
|------|------|-------|-------|
| A | 8000 | GadflyII/Qwen3-Coder-Next-NVFP4 | Orchestrator / primary coder |
| B | 8004 | txn545/Qwen3.5-122B-A10B-NVFP4 | Alternate 122B, `solo_only: true` |
| D | 8006 | txn545/Qwen3.5-122B-A10B-NVFP4 | 122B with MTP |
| E | 8007 | Sehyo/Qwen3.5-35B-A3B-NVFP4 | 35B MoE (active 3B), MTP+CUTLASS |
| F | 8009 | ykarout/Qwen3.5-9b-nvfp4 | 9B fast worker, runs alongside SlotE |
| — | 8017 | (proxy → SlotE) | vllm-think-proxy — thinking toggle layer |

OpenClaw connects SlotE via port **8017** (the think proxy), not directly to 8007.

---

## Active Configuration (as of 2026-03-31)

### SlotE — Primary Chat (Nyx)

Model: `Sehyo/Qwen3.5-35B-A3B-NVFP4`
~24GB weights, leaving ~79GB for KV cache at 0.60 gpu_memory_utilization.

```yaml
defaults:
  port: 8007
  tensor_parallel: 1
  gpu_memory_utilization: 0.60
  max_model_len: 262144        # 256K — native Qwen3.5 max
  max_num_seqs: 4
  max_num_batched_tokens: 131072  # CRITICAL — see Diagnostics

env:
  VLLM_NVFP4_MOE_FORCE_MARLIN: 0
  VLLM_USE_FLASHINFER_MOE_FP4: 0   # flashinfer issue #2776

vllm flags:
  --kv-cache-dtype fp8
  --attention-backend flashinfer
  --enable-chunked-prefill
  --load-format fastsafetensors
  --enable-prefix-caching
  --enable-auto-tool-choice
  --tool-call-parser qwen3_coder
  --reasoning-parser qwen3
  --reasoning-config '{"think_start_str": "<think>", "think_end_str": "I have to give the solution based on the thinking directly now.</think>"}'
  --chat-template unsloth.jinja
  --default-chat-template-kwargs '{"enable_thinking": false}'
  --speculative-config '{"method":"mtp","num_speculative_tokens":3}'
  --limit-mm-per-prompt '{"image": 5}'
```

See full recipe: [recipes/qwen3.5-35b-sehyo-nvfp4-mtp-slotE.yaml](recipes/qwen3.5-35b-sehyo-nvfp4-mtp-slotE.yaml)

### SlotF — Worker (optional, runs alongside SlotE)

Model: `ykarout/Qwen3.5-9b-nvfp4`
~6GB weights, `gpu_memory_utilization: 0.20` — designed to coexist with SlotE.

```yaml
defaults:
  port: 8009
  gpu_memory_utilization: 0.20
  max_model_len: 32768
  max_num_seqs: 8
  max_num_batched_tokens: 16384
```

See full recipe: [recipes/qwen3.5-9b-ykarout-nvfp4-slotF.yaml](recipes/qwen3.5-9b-ykarout-nvfp4-slotF.yaml)

### SlotB — Alternate 122B (solo only)

Model: `txn545/Qwen3.5-122B-A10B-NVFP4`
Cannot run alongside any other slot — takes nearly all unified memory.

```yaml
solo_only: true
gpu_memory_utilization: 0.76
max_model_len: 65536
```

---

## vllm-think-proxy

File: [`vllm-think-proxy.py`](vllm-think-proxy.py)

A lightweight aiohttp proxy that sits between OpenClaw and SlotE (8007). OpenClaw hits port **8017**, the proxy hits 8007.

### Purpose

Qwen3.5 supports an extended thinking mode (`enable_thinking: true` in `chat_template_kwargs`). The default is thinking OFF. The proxy allows selective per-request thinking activation using a text prefix.

### How it works

- Default: all requests pass through with `enable_thinking: false` (server default)
- Prefix `//` anywhere in the last user message: proxy injects `chat_template_kwargs: {enable_thinking: true}`
- Prefix `// high` / `// medium` / `// low`: sets thinking level (budget-mapped, currently informational)
- Prefix detection only fires when the last message role is `"user"` — prevents re-triggering during tool continuation turns (where last role is `"tool"` or `"assistant"`)

### Reasoning config

The `--reasoning-config` on vLLM sets a custom end-of-think token:

```
think_end_str: "I have to give the solution based on the thinking directly now.</think>"
```

This was set to match the fine-tuning used in the Sehyo NVFP4 model.

### Running the proxy

```bash
python3 ~/vllm-think-proxy.py --port 8017 --backend http://localhost:8007
```

Or as a systemd service (see [services/](services/)).

### Multimodal content note

OpenClaw sends message `content` as a **list** of `{"type": "text", "text": "..."}` objects, not plain strings. The proxy handles both formats:

```python
if isinstance(content, str):
    # plain string path
elif isinstance(content, list):
    # iterate to find last {"type": "text"} part
```

OpenClaw also prepends a JSON metadata block to every user message:

```
Conversation info (untrusted metadata)
{"timestamp": ..., "platform": "telegram", ...}
```

The `//` prefix can appear on any line — the proxy searches all lines, not just the first.

---

## Diagnostics & Known Issues

### "Agent couldn't generate a response"

**Symptom:** OpenClaw shows "Agent couldn't generate a response" or vLLM returns a 500/context error mid-conversation.

**Cause:** Session context grew beyond `max_num_batched_tokens`. With chunked prefill enabled, vLLM processes the prompt in chunks of `max_num_batched_tokens` tokens. If the full session exceeds this, vLLM aborts.

**Fix:** Increase `max_num_batched_tokens`. Current value: `131072` (bumped from `32768`).

```yaml
max_num_batched_tokens: 131072
```

Note: `max_num_batched_tokens` is different from `max_model_len`. You can have a 256K context window but still hit issues if batched tokens is too low.

---

### Container reuse bug (params not updated after restart)

**Symptom:** After running `./run-recipe.sh ... --solo`, the running container still shows old parameters (e.g. `--max-model-len 131072` when recipe says `262144`).

**Cause:** `--solo` calls `launch-cluster.sh` which may exec into an existing `vllm_node` container rather than replacing it.

**Fix:** Always `docker kill vllm_node` first, then relaunch:

```bash
docker kill vllm_node
sleep 2
cd ~/spark-vllm-docker
./run-recipe.sh recipes/qwen3.5-35b-sehyo-nvfp4-mtp-slotE.yaml --solo &
```

**Verify new params:**
```bash
docker exec vllm_node ps aux | grep vllm | grep -v grep
```
Check for `--max-model-len 262144 --max-num-batched-tokens 131072`.

---

### FlashInfer MoE FP4 crash on SM121 (flashinfer issue #2776)

**Symptom:** Illegal memory access during CUDA graph capture when using FlashInfer MoE FP4 kernel on GB10.

**Fix:** Disable via env var:
```yaml
env:
  VLLM_USE_FLASHINFER_MOE_FP4: 0
```

This forces fallback to the CUTLASS/Marlin MoE path, which works correctly on SM121.

---

### NVFP4 quantization (vLLM PR #38126)

The `vllm-node` image from `eugr/spark-vllm-docker` includes a fix for NVFP4 quantization that is not yet in upstream vLLM. Do not use a stock vLLM image for NVFP4 models on Spark — it will produce wrong outputs or crash.

---

### MARLIN backend requirement for 122B on SM121

**Symptom:** `txn545/Qwen3.5-122B-A10B-NVFP4` fails or produces garbage output with FLASHINFER_CUTLASS MoE backend on SM121.

**Cause:** SM121 does not support the PTX instruction `cvt .e2m1x2` generated by FLASHINFER_CUTLASS.

**Fix (required for SlotB/D 122B recipes):**
```yaml
env:
  VLLM_USE_FLASHINFER_MOE_FP4: 0
  VLLM_NVFP4_GEMM_BACKEND: "marlin"
  VLLM_TEST_FORCE_FP8_MARLIN: 1
```

Reference: https://forums.developer.nvidia.com/t/361819

---

### Thinking not triggering (prefix_level=None in proxy logs)

**Symptom:** Proxy log shows `prefix_level=None` even though user typed `//`.

**Possible causes:**

1. **Last message is not user role** — if conversation is in an agentic loop (tool calls), the last message role will be `"tool"` or `"assistant"`. Proxy skips detection intentionally.

2. **`//` is in a list content block, not a string** — OpenClaw sends multimodal arrays. Check proxy handles list content (it does, as of current version).

3. **`//` appears in a non-last user message** — proxy only checks the last user message.

**Debug:** Check proxy log:
```
effort=... last_role=user content=...
```
If `last_role=user` and content shows `//`, but `prefix_level=None`, the prefix search failed. Check for leading whitespace or Unicode lookalikes.

---

### MTP (Multi-Token Prediction) speculative decoding

SlotE uses MTP with 3 speculative tokens:

```json
{"method": "mtp", "num_speculative_tokens": 3}
```

The Sehyo NVFP4 model includes MTP weights in `extra_weights.safetensors`. This provides ~1.5–2x throughput improvement for streaming use. If you see speculative decoding errors, check that the model repo has this file.

---

## Memory Budget (121 GB unified)

| Component | ~Size |
|-----------|-------|
| Qwen3.5-35B NVFP4 weights | ~24 GB |
| OS + system | ~8 GB |
| vLLM process overhead | ~5 GB |
| KV cache (0.60 utilization) | ~79 GB |
| Headroom | ~5 GB |

With SlotF alongside (0.20 utilization):
- SlotF takes ~22 GB (weights + KV cache)
- SlotE must reduce to 0.50–0.55 gpu_memory_utilization

---

## OpenClaw Model Routing

OpenClaw connects to all slots via `openclaw.json`. Key entries:

```json
"vllm-slotE": {
  "baseUrl": "http://127.0.0.1:8017/v1",   ← think proxy, not 8007
  "models": [{
    "id": "Sehyo/Qwen3.5-35B-A3B-NVFP4",
    "name": "Slot E — Qwen3.5 35B MTP+CUTLASS",
    "contextWindow": 262144
  }]
}
```

**Important:** SlotE is registered at port **8017** (think proxy). The actual vLLM instance is at 8007. If the proxy is not running, SlotE will appear offline in OpenClaw even though vLLM is healthy.

Check proxy is running:
```bash
ss -tlnp | grep 8017
# or
ps aux | grep think-proxy
```

---

## Chat Template (fix-qwen3.5-chat-template mod)

The `mods/fix-qwen3.5-chat-template` mod provides a custom `unsloth.jinja` chat template for Qwen3.5. It is applied at container launch.

Key behaviors:
- Supports tool calls with `<tool_call><function=...>` format
- Supports vision (image/video) content blocks
- `enable_thinking` kwarg controls `<think>` block generation
- Default: `enable_thinking: false` (set in `--default-chat-template-kwargs`)

---

## Persistent Scripts

All recurring/cron scripts live in `~/.openclaw/workspace/scripts/`. Never use `/tmp/` for scripts that need to survive reboots — it is wiped on restart.

Key scripts:
- `crypto_prices_fetcher.py` — Binance price fetcher
- `thai_funds_nav_fetcher.py` — SEC Thailand fund NAV
- `us_stocks_close_fetcher.py` — US market close prices (yfinance + Alpha Vantage)
- `morning_brief.py` — Daily morning summary via Telegram

---

## API Integrations

All API keys stored in `~/.config/openclaw.env` (chmod 600). Load with:

```python
from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/.config/openclaw.env"))
```

| Service | Env Var | Notes |
|---------|---------|-------|
| OpenSky Network | `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET` | OAuth2 client credentials (Keycloak). Token auto-refreshed. |
| Alpha Vantage | `ALPHA_VANTAGE_KEY` | 25 req/day free tier. Use yfinance for bulk. |
| Binance | `BINANCE_API_KEY`, `BINANCE_SECRET` | Read-only. HMAC-SHA256 signed requests. |
| SEC Thailand | `SEC_FUND_DAILY_KEY`, `SEC_FUND_FACTSHEET_KEY` | Two separate keys. HTTP 204 = no data (weekend/holiday). |

---

## Launch Checklist (after reboot)

1. Kill any stale containers:
   ```bash
   docker kill vllm_node 2>/dev/null; true
   ```

2. Launch SlotE:
   ```bash
   cd ~/spark-vllm-docker
   ./run-recipe.sh recipes/qwen3.5-35b-sehyo-nvfp4-mtp-slotE.yaml --solo &
   ```

3. Start think proxy:
   ```bash
   python3 ~/vllm-think-proxy.py --port 8017 --backend http://localhost:8007 &
   ```

4. Optionally launch SlotF (reduce SlotE gpu_memory_utilization first):
   ```bash
   ./run-recipe.sh recipes/qwen3.5-9b-ykarout-nvfp4-slotF.yaml &
   ```

5. Verify:
   ```bash
   curl -s http://localhost:8017/v1/models | python3 -m json.tool
   docker exec vllm_node ps aux | grep vllm | grep -v grep
   ```

---

## File Map

```
~/spark-vllm-docker/                    # eugr/spark-vllm-docker clone
  recipes/
    qwen3.5-35b-sehyo-nvfp4-mtp-slotE.yaml
    qwen3.5-9b-ykarout-nvfp4-slotF.yaml
    qwen3.5-122b-nvfp4-slotB.yaml
  mods/
    fix-qwen3.5-chat-template/          # unsloth.jinja + mod.yaml

~/vllm-think-proxy.py                   # Think toggle proxy (8017 → 8007)

~/.openclaw/openclaw.json               # OpenClaw main config
~/.openclaw/workspace/
  TOOLS.md                              # OpenClaw tool reference (Nyx reads this)
  scripts/                              # Persistent cron scripts
  skills/
    flight-tracker/scripts/track.py    # OpenSky flight tracker (OAuth2)
    task_template.py                   # Base template for all task scripts

~/.config/openclaw.env                 # API keys (chmod 600)
~/.config/claude.env                   # Claude Code API keys (chmod 600)
```
