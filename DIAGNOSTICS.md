# Diagnostics Quick Reference

Fast lookup for common issues. For full context, see [README.md](README.md).

---

## Token counts not showing in clawmetry

**Symptom:** All usage fields in session files are zero. Clawmetry shows 0 tokens.

**Cause:** vLLM doesn't return token usage in streaming responses by default. OpenClaw never sees the counts.

**Fix:** The `vllm-think-proxy.py` injects `stream_options: {include_usage: true}` into every streaming request. vLLM then appends a usage chunk before `[DONE]` and OpenClaw records it.

Verify the proxy is injecting it:
```bash
curl -s http://localhost:8017/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Sehyo/Qwen3.5-35B-A3B-NVFP4","messages":[{"role":"user","content":"hi"}],"max_tokens":5,"stream":true}' \
  | grep '"usage"'
```
Should return a line with `prompt_tokens`, `completion_tokens`, `total_tokens`.

If the proxy is down: `systemctl --user restart vllm-think-proxy.service`

---

## Is vLLM actually running with the right params?

```bash
docker exec vllm_node ps aux | grep vllm | grep -v grep
```

Look for: `--max-model-len 262144 --max-num-batched-tokens 131072`

If you see `131072` for max-model-len or `32768` for max-num-batched-tokens, the container was not restarted properly. Fix:

```bash
docker kill vllm_node && sleep 2
cd ~/spark-vllm-docker
./run-recipe.sh recipes/qwen3.5-35b-sehyo-nvfp4-mtp-slotE.yaml --solo &
```

---

## Is the think proxy running?

```bash
ss -tlnp | grep 8017
```

If nothing: `python3 ~/vllm-think-proxy.py --port 8017 --backend http://localhost:8007 &`

Check proxy logs for request activity:
```bash
# If launched in a terminal, scroll up
# If running as a service, check journalctl
journalctl -u vllm-think-proxy -n 50
```

---

## Is OpenClaw seeing SlotE?

SlotE is registered at port 8017 in openclaw.json. If proxy is down, SlotE appears offline.

```bash
curl -s http://localhost:8017/v1/models
curl -s http://localhost:8007/v1/models  # vLLM direct (bypass proxy)
```

---

## Is vLLM done loading?

vLLM takes 1–3 minutes to load NVFP4 models with fastsafetensors. Check:

```bash
docker logs vllm_node --tail 20
```

Look for: `INFO:     Application startup complete.` or `Uvicorn running on http://0.0.0.0:8007`

---

## "Agent couldn't generate a response"

Session context exceeded `max_num_batched_tokens`. Current value: 131072.

Fix: start a new conversation (resets context). Or increase `max_num_batched_tokens` in recipe and restart container.

---

## Thinking not working (`//` prefix has no effect)

1. Check proxy log: does it show `prefix_level=high/medium/low`?
   - If `prefix_level=None`: prefix was not detected. Check the actual content being sent.
   - If detected but behavior unchanged: `chat_template_kwargs` not reaching vLLM.

2. Check last message role:
   - Proxy only detects prefix when `last_role == "user"`.
   - In an agentic loop with tool calls, last role may be `"tool"` — thinking is intentionally skipped.

3. Verify `enable_thinking` is reaching vLLM:
   ```bash
   # Check proxy logs for:
   # Thinking ENABLED (prefix_level=high, effort=None)
   ```

---

## FlashInfer CUDA graph capture crash

```
CUDA error: an illegal memory access was encountered
```

During model load / CUDA graph capture, related to FlashInfer MoE FP4.

Fix: ensure `VLLM_USE_FLASHINFER_MOE_FP4: 0` is set in recipe env. Already set in current SlotE recipe.

---

## Memory pressure / OOM

Check current memory usage:
```bash
free -h
# or
nvidia-smi  # shows unified memory on GB10
```

If running SlotE + SlotF together and getting OOM:
- SlotE: reduce `gpu_memory_utilization` to 0.50–0.55
- SlotF: already at 0.20
- Combined: 0.70–0.75 of 121 GB ≈ 85–90 GB — should be fine

If still OOM: kill SlotF and run SlotE `--solo`.

---

## ICAO24 resolution (flight tracker)

Never hardcode or guess ICAO24 from a callsign. Resolve live:

```bash
~/.openclaw/bin/python3 ~/.openclaw/workspace/skills/flight-tracker/scripts/track.py --callsign THA416
```

The script queries `/states/all`, filters by callsign string match, and returns the live ICAO24 + position. Using hardcoded ICAO24 (e.g. from US registry for a non-US airline) will return 404 or wrong aircraft.

---

## OpenSky OAuth2 token failure

```
invalid_client
```

Check that `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` in `~/.config/openclaw.env` are correct. OpenSky moved from basic auth to OAuth2 (Keycloak) in 2025. The token manager handles auto-refresh; a cold start will always fetch a new token.

---

## SEC Thailand API — HTTP 204

HTTP 204 from the SEC Thailand fund API means no data for that date. This is normal for weekends and Thai public holidays. Not an error.

---

## Model output quality degraded / wrong format

Check:
1. Is the correct chat template active? (`unsloth.jinja` from the `fix-qwen3.5-chat-template` mod)
2. Is `enable_thinking` accidentally `true` when it should be `false`? Check `--default-chat-template-kwargs`.
3. Is the reasoning parser active? (`--reasoning-parser qwen3`) — strips `<think>...</think>` blocks from the visible response.
4. Is tool call parser set? (`--tool-call-parser qwen3_coder`) — required for function calling to work.
