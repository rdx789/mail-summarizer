#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Mail Summarizer (skills variant) — wrapper script
# Same shape as ../mail/run_mail_summarizer.sh, but LLM_BACKEND values match
# the local-llm-backend skill's naming: "ollama" or "openai_compatible"
# (not "llama_server").
# ─────────────────────────────────────────────────────────────────────────────

MAIL_DIR="/Users/dron/PyCharmMiscProject/sandbox/mail-summarizer"
PYTHON="$MAIL_DIR/.venv/bin/python"
MIN_GAP=3600    # minimum seconds between runs (1 hour); prevents double-fire on quick restarts
LOG_FILE="$HOME/Library/Logs/mail-summarizer-skills.log"
GAP_FILE="$HOME/.mail_summarizer_skills_last_run"

# ── Load LLM_BACKEND and its config from .env — single source of truth with main.py ──
set -a
# shellcheck disable=SC1091
source "$MAIL_DIR/.env"
set +a
if [ "$LLM_BACKEND" != "ollama" ] && [ "$LLM_BACKEND" != "openai_compatible" ]; then
    echo "ERROR: LLM_BACKEND='$LLM_BACKEND' in $MAIL_DIR/.env must be 'ollama' or 'openai_compatible' — aborting."
    exit 1
fi
if [ "$LLM_BACKEND" = "ollama" ] && { [ -z "$OLLAMA_URL" ] || [ -z "$OLLAMA_MODEL" ]; }; then
    echo "ERROR: OLLAMA_URL / OLLAMA_MODEL not set in $MAIL_DIR/.env — aborting."
    exit 1
fi
if [ "$LLM_BACKEND" = "openai_compatible" ] && [ -z "$LLAMA_MODEL_PATH" ]; then
    echo "ERROR: LLAMA_MODEL_PATH not set in $MAIL_DIR/.env — aborting."
    exit 1
fi
LLAMA_SERVER_BIN="${LLAMA_SERVER_BIN:-/opt/homebrew/bin/llama-server}"
LLAMA_PORT=8080

# ── Redirect all output to log file ──────────────────────────────────────────
exec >> "$LOG_FILE" 2>&1
echo ""
echo "═══════════════════════════════════════════════════"
echo "  Mail Summarizer (skills)  $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════"

# ── Minimum gap check ─────────────────────────────────────────────────────────
NOW=$(date +%s)
if [ -f "$GAP_FILE" ]; then
    LAST=$(cat "$GAP_FILE")
    ELAPSED=$(( NOW - LAST ))
    if (( ELAPSED < MIN_GAP )); then
        echo "Last run was ${ELAPSED}s ago (min gap ${MIN_GAP}s) — skipping."
        exit 0
    fi
fi
echo "$NOW" > "$GAP_FILE"

LLAMA_STARTED_BY_US=false

if [ "$LLM_BACKEND" = "ollama" ]; then
    # ── Load the Ollama model into memory before the run ──────────────────────
    echo "Loading $OLLAMA_MODEL into Ollama..."
    if ! curl -sf "$OLLAMA_URL" \
            -d "{\"model\":\"$OLLAMA_MODEL\",\"prompt\":\"\",\"keep_alive\":\"30m\"}" \
            -o /dev/null; then
        echo "ERROR: could not reach Ollama at $OLLAMA_URL — aborting."
        exit 1
    fi
    echo "  → $OLLAMA_MODEL loaded."
else
    # ── Start llama-server if not already running ──────────────────────────────
    if curl -sf "http://localhost:$LLAMA_PORT/health" | grep -q '"status":"ok"'; then
        echo "llama-server already responding on :$LLAMA_PORT — reusing."
    else
        echo "Starting llama-server..."
        # No --parallel flag: leave it at llama-server's default (4 slots),
        # which also makes --ctx-size apply per-slot rather than being
        # divided across slots. 5 slots measured slower wall-clock than 4
        # on this hardware (memory-bandwidth contention) — don't add
        # --parallel 5 back without re-benchmarking.
        "$LLAMA_SERVER_BIN" \
            --model "$LLAMA_MODEL_PATH" \
            --port  "$LLAMA_PORT" \
            --ctx-size 16384 \
            --n-gpu-layers 99 \
            --flash-attn on \
            &
        LLAMA_PID=$!
        LLAMA_STARTED_BY_US=true

        echo "Waiting for llama-server to be ready..."
        READY=false
        for i in $(seq 1 120); do
            if curl -sf "http://localhost:$LLAMA_PORT/health" | grep -q '"status":"ok"'; then
                echo "  → ready after ${i}s."
                READY=true
                break
            fi
            sleep 1
        done

        if ! $READY; then
            echo "ERROR: llama-server did not become ready in 120s — aborting."
            kill "$LLAMA_PID" 2>/dev/null
            exit 1
        fi
    fi
fi

# ── Run main.py ───────────────────────────────────────────────────────────────
echo "Running main.py..."
cd "$MAIL_DIR" || exit 1
"$PYTHON" main.py
EXIT_CODE=$?
echo "main.py finished (exit $EXIT_CODE)."

if [ "$LLM_BACKEND" = "ollama" ]; then
    # ── Unload the model from Ollama so it frees GPU/RAM between runs ─────────
    echo "Unloading $OLLAMA_MODEL from Ollama..."
    curl -sf "$OLLAMA_URL" \
        -d "{\"model\":\"$OLLAMA_MODEL\",\"prompt\":\"\",\"keep_alive\":0}" \
        -o /dev/null
    echo "  → $OLLAMA_MODEL unloaded."
elif $LLAMA_STARTED_BY_US; then
    # ── Stop llama-server if we started it ─────────────────────────────────────
    echo "Shutting down llama-server (PID $LLAMA_PID)..."
    kill "$LLAMA_PID" 2>/dev/null
    wait "$LLAMA_PID" 2>/dev/null
    echo "llama-server stopped."
fi

echo "Done  $(date '+%Y-%m-%d %H:%M:%S')"
exit $EXIT_CODE
