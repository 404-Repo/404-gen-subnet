#!/usr/bin/env bash
set -euo pipefail

# Activate the `judge-env` Conda environment before invoking this script.

MODEL=${MODEL:-THUDM/GLM-4.1V-9B-Thinking}
REVISION=${REVISION:-17193d2147da3acd0da358eb251ef862b47e7545}
PORT=${PORT:-8095}
API_KEY=${API_KEY:-local}
VLLM_ARGS=${VLLM_ARGS:-"--max-model-len 8096 --tensor-parallel-size 1 --gpu-memory-utilization 0.9 --max_num_seqs 2"}

vllm serve "$MODEL" \
    --revision "$REVISION" \
    --port "$PORT" \
    --api-key "$API_KEY" \
    $VLLM_ARGS
