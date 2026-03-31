#!/bin/bash
# launch-slotE.sh — Clean restart of SlotE (Nyx / Qwen3.5-35B)
# Kills existing container to ensure new params take effect.
# Usage: ./launch-slotE.sh [--with-slotF]

set -e

RECIPE_DIR="$HOME/spark-vllm-docker"
SLOTE_RECIPE="recipes/qwen3.5-35b-sehyo-nvfp4-mtp-slotE.yaml"
SLOTF_RECIPE="recipes/qwen3.5-9b-ykarout-nvfp4-slotF.yaml"

echo "[launch-slotE] Killing existing vllm_node container..."
docker kill vllm_node 2>/dev/null || echo "(none running)"
sleep 2

echo "[launch-slotE] Starting SlotE..."
cd "$RECIPE_DIR"
./run-recipe.sh "$SLOTE_RECIPE" --solo &

if [[ "$1" == "--with-slotF" ]]; then
    echo "[launch-slotE] Waiting 30s before starting SlotF..."
    sleep 30
    ./run-recipe.sh "$SLOTF_RECIPE" &
fi

echo "[launch-slotE] Waiting for vLLM to start..."
sleep 15

echo "[launch-slotE] Verifying params..."
docker exec vllm_node ps aux | grep vllm | grep -v grep | tr ' ' '\n' | grep -E 'max-model|max-num-batched|port' | head -10

echo "[launch-slotE] Done. Check logs: docker logs vllm_node --tail 30"
