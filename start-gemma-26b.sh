#!/usr/bin/env bash
# start-gemma-26b.sh
#
# Starts Gemma 4 26B-A4B (MoE, 128 experts, 8 active) with an optimized
# expert placement on the Radeon RX 6700 XT (12 GB, RDNA2, Vulkan).
#
# Measured on this card (llama.cpp Vulkan 2.53.0, 32k context, q8_0 KV):
#   -ncmoe 15 -> 34.5 tok/s generation, prefill 803 tok/s, ~10.2 GB VRAM
#   -ncmoe 13 -> 36.6 tok/s generation, prefill 840 tok/s, ~11.0 GB VRAM  (default)
#   -ncmoe 11 -> 39.1 tok/s generation, prefill 873 tok/s, ~11.2 GB VRAM
#   -ncmoe  9 -> 42.2 tok/s generation, prefill 906 tok/s, ~11.5 GB VRAM  (too tight)
#
# Warning: beyond ~11.5 GB of VRAM usage prompt processing collapses
# (prefill drops from ~900 to ~160 tok/s). The desktop normally uses ~1.4 GB.
# When in doubt increase -ncmoe: less VRAM, slightly slower.
#
# To run the model inside Bionic instead of through this script:
#   - Load it with 'Auto' placement -> 33.5 tok/s at 16k context. NOTE: pinning
#     the context length (16k OR 32k) drops throughput to 21.7 tok/s, so the
#     automatic placement clearly wins.
#   - The load dialog has an MoE-only slider 'Number of CPU Expert Layers'
#     (LM Studio field gpu.numCpuExpertLayersRatio). Set to ~13 the in-app
#     performance approaches the numbers of this script.
#   - A fixed 32k context at full speed is only available here:
#     840 tok/s prefill instead of ~700, and 36.6 instead of 33.5 tok/s.
#
# Ports: this server defaults to http://127.0.0.1:8081. LM Studio's own local server
# is the owner of 127.0.0.1:8080 (Settings -> Developer -> Server Port), so both can
# run at the same time.
#
# Usage:
#   ./start-gemma-26b.sh                 # default: -ncmoe 13, 32k context, port 8081
#   ./start-gemma-26b.sh --ncmoe 15      # safer, ~10.2 GB VRAM
#   ./start-gemma-26b.sh --ctx 16384     # smaller context = more headroom
#   ./start-gemma-26b.sh --think off     # no reasoning (short, fast answers)
#   ./start-gemma-26b.sh --dry-run       # print the command only
#
# Thinking: enabled by default. The reasoning steps go to 'reasoning_content',
# the actual answer to 'content'. Even a short answer then burns ~250 thinking
# tokens, so set max_tokens >= 512 - otherwise 'content' stays empty and the
# entire output ends up in the reasoning. For short answers use: --think off
#
# Stop with Ctrl+C.

set -euo pipefail

BACKENDS_DIR="${BACKENDS_DIR:-$HOME/.lmstudio/extensions/backends}"
DEFAULT_MODEL="$HOME/.lmstudio/models/lmstudio-community/gemma-4-26B-A4B-it-QAT-GGUF/gemma-4-26B-A4B-it-QAT-Q4_0.gguf"

MODEL="${MODEL:-$DEFAULT_MODEL}"
NCMOE="${NCMOE:-13}"
CTX="${CTX:-32768}"
PORT="${PORT:-8081}"
HOST="${HOST:-127.0.0.1}"
THINK="${THINK:-}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
DRY_RUN=0

usage() {
  sed -n '2,/^set -/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --model PATH     GGUF file (default: Gemma 4 26B-A4B QAT Q4_0 from LM Studio)
  --ncmoe N        keep the experts of the first N layers in RAM (0-30, default 13)
  --ctx N          context length (default 32768)
  --port N         server port (default 8081)
  --think MODE     reasoning: on, off or auto (default: on, the llama.cpp default)
  --dry-run        print the command instead of starting the server
  -h, --help       this help

Environment variables: MODEL, NCMOE, CTX, PORT, HOST, THINK, EXTRA_ARGS, BACKENDS_DIR
Extra llama.cpp flags can be appended through EXTRA_ARGS, e.g.:
  EXTRA_ARGS="--parallel 1" ./start-gemma-26b.sh
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --model)   MODEL="${2:?--model needs a path}"; shift 2 ;;
    --ncmoe)   NCMOE="${2:?--ncmoe needs a number}"; shift 2 ;;
    --ctx)     CTX="${2:?--ctx needs a number}"; shift 2 ;;
    --port)    PORT="${2:?--port needs a number}"; shift 2 ;;
    --think)   THINK="${2:?--think needs on, off or auto}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; echo "Help: $0 --help" >&2; exit 2 ;;
  esac
done

case "$NCMOE" in ''|*[!0-9]*) echo "Error: --ncmoe must be a number (got: '$NCMOE')" >&2; exit 2 ;; esac
case "$CTX"   in ''|*[!0-9]*) echo "Error: --ctx must be a number (got: '$CTX')" >&2; exit 2 ;; esac
case "$PORT"  in ''|*[!0-9]*) echo "Error: --port must be a number (got: '$PORT')" >&2; exit 2 ;; esac
if [ "$NCMOE" -gt 30 ]; then echo "Error: --ncmoe > 30 is pointless (the model has 30 layers)" >&2; exit 2; fi
if [ -n "$THINK" ]; then
  case "$THINK" in
    on|off|auto) ;;
    *) echo "Error: --think expects on, off or auto (got: '$THINK')" >&2; exit 2 ;;
  esac
fi

# --- Locate the runtime: the newest Vulkan build shipped with LM Studio ---
BACKEND="$(ls -d "$BACKENDS_DIR"/llama.cpp-linux-x86_64-vulkan-* 2>/dev/null | sort -V | tail -1 || true)"
if [ -z "$BACKEND" ] || [ ! -x "$BACKEND/llama-server" ]; then
  echo "Error: no llama.cpp Vulkan runtime found in $BACKENDS_DIR" >&2
  echo "       Install a Vulkan runtime in LM Studio under 'Runtime'." >&2
  exit 1
fi

if [ ! -f "$MODEL" ]; then
  echo "Error: model file not found: $MODEL" >&2
  echo "       Install it in LM Studio or run: lms get google/gemma-4-26b-a4b-qat" >&2
  exit 1
fi

# --- Report the VRAM state of the largest dedicated GPU ---
vram_report() {
  local t u tv uv best_total=0 best_used=0
  for t in /sys/class/drm/card*/device/mem_info_vram_total; do
    [ -r "$t" ] || continue
    u="${t%_total}_used"
    read -r tv < "$t" 2>/dev/null || continue
    uv=0; [ -r "$u" ] && { read -r uv < "$u" || uv=0; }
    if [ "$tv" -gt "$best_total" ]; then best_total=$tv; best_used=$uv; fi
  done
  if [ "$best_total" -gt 0 ]; then
    awk -v u="$best_used" -v t="$best_total" 'BEGIN {
      printf "VRAM before start: %.2f GB used of %.2f GB (free: %.2f GB)\n",
             u/1073741824, t/1073741824, (t-u)/1073741824 }'
  fi
}
vram_report
if [ "$NCMOE" -lt 11 ]; then
  echo "Warning: -ncmoe $NCMOE uses more than ~11.5 GB. If the desktop needs"
  echo "         memory on top of that, prompt processing collapses (prefill ~160"
  echo "         tok/s instead of ~900). On slow answers to long inputs: --ncmoe 13 or 15."
fi
if [ "$NCMOE" -ge 25 ]; then
  echo "Note: -ncmoe $NCMOE is close to 'all experts in RAM' (slower, but lots of VRAM free)."
fi

CMD=(
  "$BACKEND/llama-server"
  --model "$MODEL"
  --n-gpu-layers 99
  --n-cpu-moe "$NCMOE"
  --ctx-size "$CTX"
  --flash-attn on
  --cache-type-k q8_0
  --cache-type-v q8_0
  --batch-size 2048
  --ubatch-size 512
  --threads 8
  --load-mode none
  --host "$HOST"
  --port "$PORT"
)
if [ -n "$THINK" ]; then
  CMD+=( --reasoning "$THINK" )
fi
if [ -n "$EXTRA_ARGS" ]; then
  # shellcheck disable=SC2206
  CMD+=( $EXTRA_ARGS )
fi

echo
echo "Starting Gemma 4 26B-A4B  (ncmoe=$NCMOE, context=$CTX, KV=q8_0)"
echo "Runtime: $(basename "$BACKEND")"
echo "Server:  http://$HOST:$PORT   (OpenAI-compatible under /v1)"
if [ "$THINK" = "off" ]; then
  echo "Thinking: off"
else
  echo "Thinking: on - reasoning steps go to 'reasoning_content'. Set max_tokens"
  echo "          >= 512, otherwise 'content' stays empty. For short answers: --think off"
fi
echo "Stop with Ctrl+C"
echo

if [ "$DRY_RUN" -eq 1 ]; then
  printf 'LD_LIBRARY_PATH=%q \\\n' "$BACKEND"
  printf '  %q' "${CMD[@]}"
  printf '\n'
  exit 0
fi

export LD_LIBRARY_PATH="$BACKEND${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
cd "$BACKEND"
exec "${CMD[@]}"
