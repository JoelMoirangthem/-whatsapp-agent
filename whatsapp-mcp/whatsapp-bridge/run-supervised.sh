#!/usr/bin/env bash
# Keeps the WhatsApp bridge alive and re-presenting a FRESH QR code until the
# account is paired. whatsmeow gives up after 3 QR cycles (exit code 0), so
# only an explicit Ctrl+C/SIGTERM to this script ends the loop.
cd "$(dirname "$0")"

# Live-event integration: push inbound messages to the agent-service SSE bus.
export WEBHOOK_ENABLED="${WEBHOOK_ENABLED:-true}"
export WEBHOOK_URL="${WEBHOOK_URL:-http://127.0.0.1:8100/internal/webhook}"
while true; do
    ./whatsapp-bridge
    code=$?
    # 130 = SIGINT (Ctrl+C), 143 = SIGTERM — deliberate stops.
    if [ "$code" -eq 130 ] || [ "$code" -eq 143 ]; then
        echo "[supervisor] stopped deliberately (code=$code)"
        exit 0
    fi
    echo "[supervisor] bridge exited (code=$code) — restarting with a fresh QR in 5s..."
    sleep 5
done
