# DGX Spark Inference Benchmarks

Hardware: NVIDIA DGX Spark (GB10), 121 GiB unified memory, 273 GB/s bandwidth, SM 12.1  
Stack: vLLM 0.18.2rc1.dev74+g71a9125c6.d20260403, CUDA 13.2, NVFP4 + Marlin backend

---

## Summary Table

| Model | Type | Weights | Active params/tok | tok/s | TTFT | Context | Notes |
|-------|------|---------|-------------------|-------|------|---------|-------|
| Gemma 4 26B-A4B NVFP4 | MoE | 15.67 GiB | 3.8B | **52.3** | <100ms | 131K (can do 262K) | Sweet spot for GB10 |
| Qwen3.5 35B-A3B NVFP4 | MoE | ~8 GiB | 3B | ~70+ | <50ms | 32K | SlotE, fastest decode |
| Gemma 4 31B NVFP4 | Dense | 30.97 GiB | 31B | **7.8** | 2.6s | 110K (KV limit) | Do not use |
| Qwen3.5 122B NVFP4 | MoE | ~65 GiB | ~22B | ~16 | ~500ms | 32K | SlotB, good reasoning |

---

## Gemma 4 26B-A4B NVFP4 (bg-digitalservices/Gemma-4-26B-A4B-it-NVFP4)

**Recipe:** `recipes/gemma4-26b-a4b-nvfp4-slotG.yaml`  
**Port:** 8008 (SlotG)  
**Config:** `gpu_memory_utilization=0.60`, `max_model_len=131072` (recipe updated to 262144, not yet restarted)

| Metric | Value |
|--------|-------|
| Weights loaded | 15.67 GiB |
| Active params/token | 3.8B (8 of 128 experts) |
| Decode speed | 52.3 tok/s |
| TTFT (short prompt) | <100ms |
| TTFT (long prompt) | ~1-2s |
| Max context | 262K tokens (KV headroom allows it) |
| KV cache allocated | ~40 GiB at 0.60 util |

**Theoretical max:** 15.67 GiB × 273 GB/s ≈ 58 tok/s — running at ~90% bandwidth efficiency.

---

## Gemma 4 31B Dense NVFP4 (nvidia/Gemma-4-31B-IT-NVFP4)

**Recipe:** `recipes/gemma4-31b-nvfp4-slotG.yaml`  
**Config:** `gpu_memory_utilization=0.90`, `max_model_len=110000`, `max_num_seqs=2`

| Metric | Value |
|--------|-------|
| Weights loaded | 30.97 GiB |
| Active params/token | 31B (all) |
| Decode speed | 7.8 tok/s |
| TTFT (36-token prompt) | 2.6s |
| Max context | 110K (KV limit: 48.11 GiB / 105K tokens at 0.90 util) |
| torch.compile time | 58.97s |
| Autotuner (flashinfer) | 163s |
| Total boot time | ~9 minutes |

**Theoretical max:** 30.97 GiB × 273 GB/s ≈ 8.8 tok/s — running at ~89% bandwidth efficiency.

**Verdict: do not use.** Slower than Qwen3.5 122B MoE despite being 4x smaller total params.
The 26B MoE is 6.7x faster with longer context and near-instant TTFT.

---

## Key Insights

**Dense models are a poor fit for GB10.** This SoC is memory-bandwidth-limited with no native FP4
compute (Marlin handles weight dequant). What determines tok/s is **active parameters per decode step**,
not total parameter count:

- Dense model: every weight is touched every token
- MoE model: only the active experts (small fraction) are loaded per token

Ranking by effective compute per token (lower = faster):
1. Qwen3.5 35B-A3B: ~3B active → fastest
2. Gemma 4 26B-A4B: ~3.8B active → 52 tok/s
3. Qwen3.5 122B: ~22B active → ~16 tok/s
4. Gemma 4 31B dense: 31B active → 7.8 tok/s (worst — beaten by a 122B MoE)

**On GB10: prefer MoE with small active-param count over any dense model.**

---

## KV Cache Notes

- FP8 KV cache used across all models
- GB10 unified memory means no separate VRAM — model weights + KV cache share the 121 GiB pool
- At `gpu_memory_utilization=0.90`: ~48 GiB available for KV after 31B dense loads
- At `gpu_memory_utilization=0.60`: ~40 GiB available for KV after 26B MoE loads (plenty)
- KV cache is the bottleneck for long-context dense models, not a concern for MoE

---

*Last updated: 2026-04-04*
