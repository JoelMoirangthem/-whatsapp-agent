# Deployment Runbook

Three deployment modes, from simplest to production-like.

## 1. Local development (no Docker)

Prerequisites: Go 1.25+, Python 3.11+ with [uv](https://docs.astral.sh/uv/), FFmpeg optional.

```bash
# Terminal 1 — Go bridge (prints QR on first run)
cd whatsapp-mcp/whatsapp-bridge
go run .
# → Scan the QR with WhatsApp → Settings → Linked Devices → Link a Device
# → Token written to store/.bridge-token

# Terminal 2 — Agent service
cd agent-service
cp .env.example .env          # add your AGENTROUTER_API_KEY
uv sync --extra dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8100

# Verify the whole chain
uv run python scripts/e2e_check.py --api-url http://127.0.0.1:8100 \
    --token "$(cat data/.agent-token)"
```

Approval UI: http://127.0.0.1:8100/approvals

## 2. Docker Compose (recommended for pilots)

```bash
cd Whatsapp-Agent/
cp .env.compose.example .env
$EDITOR .env                  # API key + two long random tokens
docker compose up -d --build

# Pair WhatsApp once:
docker compose logs -f bridge # scan QR from the logs

# Health + E2E verification
curl -s http://127.0.0.1:8100/health | jq
uv run --project agent-service python agent-service/scripts/e2e_check.py \
    --api-url http://127.0.0.1:8100 --token "$AGENT_SERVICE_TOKEN"
```

What compose sets up:

| Service | Port (host) | Volume | Notes |
|---|---|---|---|
| `bridge` | 127.0.0.1:8080 | `wa-store:/data/store` | SQLite session + message archive; QR pairing via logs |
| `agent` | 127.0.0.1:8100 | same volume, **read-only** | reaches bridge over internal network |

The shared token is injected via `WHATSAPP_BRIDGE_TOKEN` env into both services — the bridge accepts it directly (auth.go) and the agent sends it as its bearer credential.

## 3. Cloud deployment

Same compose file behind a reverse proxy (Caddy/Traefik/nginx) that provides:

1. TLS termination.
2. Real authentication in front of `/agents/*` and `/approvals` (the built-in bearer token alone is fine machine-to-machine, but add SSO/OAuth for human UI access).
3. No direct exposure of the bridge port — keep it off the public interface entirely; only the agent service needs to reach it.

Managed secrets: inject `AGENTROUTER_API_KEY` / tokens via your platform's secret manager instead of `.env`.

## Pairing & re-pairing

- **First pair:** start the bridge, scan QR from logs (`docker compose logs -f bridge` or terminal). Session persists in the `wa-store` volume across restarts and image updates.
- **Re-pair** (session broken / StreamReplaced): stop bridge, remove only `whatsapp.db` from the volume (`docker run --rm -v whatsapp-agent_wa-store:/d alpine rm /d/store/whatsapp.db`), restart, scan again. `messages.db` history survives.
- **Unlink everything:** WhatsApp app → Linked Devices → remove device; then wipe volumes if decommissioning.

## Monitoring & alerting

Minimum viable setup:

```yaml
# append to docker-compose.yml or a monitoring override file
  uptime-kuma:
    image: louislam/uptime-kuma:1
    ports: ["127.0.0.1:3001:3001"]
    volumes: [uptime-data:/app/data]
volumes: { uptime-data: {} }
```

Monitors to configure:
- HTTP(s) `http://agent:8100/health` — expect `"status": "ok"`.
- TCP port of bridge 8080 (liveness).
- Log alert: grep `guardrail_blocked` spikes; `action_failed` any occurrence.

For Prometheus/Grafana shops: scrape `/health` via json_exporter, ship container logs to Loki, dashboard on audit events.

## Backup & restore

```bash
# Backup (stop writes first for consistency)
docker compose stop agent
docker run --rm -v whatsapp-agent_wa-store:/src -v "$PWD/backups":/b alpine \
    tar czf /b/wa-store-$(date +%F).tgz -C /src store
docker compose start agent
```

Restore = extract archive into a fresh volume. The archive contains the session (account access!) — encrypt backups.

## Upgrade procedure

```bash
git pull                      # both repos
docker compose build
docker compose up -d          # session + history preserved
uv run --project agent-service pytest -q   # tests still green
```

## Incident response

| Symptom | Action |
|---|---|
| Unexpected message sent | Check `/audit` for `action_approved` events and acting user; rotate `AGENT_SERVICE_TOKEN`; review approval UI access |
| Injection attempts spiking | Raise input block sensitivity (already hard-blocking at ≥2); inspect sources in audit detail |
| Bridge session errors (409/LTHash) | See whatsapp-mcp README "App State / LTHash Conflicts" — re-pair per above |
| Key compromised | Rotate AgentRouter key in `.env`, `docker compose up -d` (restart re-reads env) |
