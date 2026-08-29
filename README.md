# WhatsApp Agent — Approval-Gated AI for WhatsApp

> **Bridge + Agent + MCP in one monorepo.** A production-ready stack that lets an LLM read and reason over your WhatsApp history, but **never** sends or deletes a message without explicit human approval.

[![Docker](https://img.shields.io/badge/docker-compose-ready-blue?logo=docker)](./docker-compose.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](./agent-service)
[![Go 1.25+](https://img.shields.io/badge/go-1.25+-00ADD8)](./whatsapp-mcp/whatsapp-bridge)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./whatsapp-mcp/LICENSE)

**Live repo:** `https://github.com/JoelMoirangthem/-whatsapp-agent`

---

## Why this exists

Most WhatsApp AI tools give the model direct send access. This one doesn't.

- **Safety contract:** `send_message` / `delete_message` / calls never execute directly. The LLM proposes → a `pending` action is created (SQLite, 5-min TTL) → you **Approve / Reject** in the UI. No other content filtering.
- **Full history locally:** Every inbound message is archived to `messages.db` via the Go bridge (whatsmeow). Reads are served from SQLite; the bridge only needed for writes.
- **Streaming + voice:** Token-streamed answers (SSE), sub-2s latency budget, and optional ElevenLabs TTS with chunked audio streaming.

---

## Architecture

```
                ┌─────────────────────────────────────────────┐
     User ─────►│  Agent Service (FastAPI :8100)              │
   POST /agents/whatsapp[/stream]   LLM via AgentRouter       │
                │   ┌──────────┴───────────┐                │
                │   ▼                      ▼                │
                │ read-only tools      mutating tools       │
                │ list_chats/          send_message/        │
                │ get_messages/        delete_message/      │
                │ search_chats         initiate_call        │
                │   │                      │                │
                │   ▼                      ▼                │
                │ answer (streamed)   pending → approve →   │
                │                     bridge REST → WhatsApp│
                └──────────┬──────────────────┬──────────────┘
                           │ REST :8080      │ webhook
                ┌──────────▼──────────────────▼──────────────┐
                │  Go Bridge (whatsmeow, :8080)              │
                │  • WebSocket to WhatsApp Web               │
                │  • SQLite: whatsapp.db + messages.db       │
                │  • Media, reactions, calls, typing, webhooks│
                └─────────────────────────────────────────────┘
```

**Two SQLite DBs:**

- `whatsapp.db` — whatsmeow session/LID map (opaque)
- `messages.db` — our archive (chats, messages, reactions, calls)

---

## Project Structure

```
whatsapp-agent/
├── docker-compose.yml          # full-stack: bridge + agent (recommended)
├── .env.compose.example        # copy to .env, fill keys
├── agent-service/              # FastAPI + LLM orchestration
│   ├── app/ {main.py, agent.py, bridge.py, llm.py, voice.py, supervisor.py}
│   ├── app/static/             # WhatsApp-Web-style UI (chats, approvals, pairing, voice)
│   ├── tests/ & pyproject.toml
│   └── Dockerfile
├── whatsapp-mcp/               # fork of verygoodplugins/whatsapp-mcp
│   ├── whatsapp-bridge/ (Go)   # REST + websockets + store
│   └── whatsapp-mcp-server/ (Python) # MCP tools for Claude/Cursor
└── docs/
```

See sub-readmes: [`agent-service/README.md`](./agent-service/README.md) · [`whatsapp-mcp/README.md`](./whatsapp-mcp/README.md)

---

## Quick Start (Docker — Recommended)

### 1. Prerequisites
- Docker + `docker compose` v2
- An [AgentRouter](https://agentrouter.org) API key (`sk-...`)
- (Optional) ElevenLabs API key for voice

### 2. Configure
```bash
cp .env.compose.example .env
# edit .env:
#   AGENTROUTER_API_KEY=sk-...
#   WHATSAPP_BRIDGE_TOKEN=$(openssl rand -hex 32)
#   AGENT_SERVICE_TOKEN=$(openssl rand -hex 32)
#   ELEVENLABS_API_KEY=... (optional)
```

### 3. Run
```bash
docker compose up -d --build
docker compose logs -f bridge   # watch for QR
```

Scan the QR in **WhatsApp → Settings → Linked Devices → Link a Device**. First sync can take minutes.

### 4. Open the UI
- **App:** http://127.0.0.1:8100/ — Chats, copilot composer, approvals, voice, pairing
- **Approvals:** http://127.0.0.1:8100/approvals
- **Health:** http://127.0.0.1:8100/health → `{bridge_up, whatsapp_connected, latency {p50,p95}}`
- **Pair:** http://127.0.0.1:8100/pair — live QR

### 5. Try it
```bash
# get service token (auto-generated to volume)
TOKEN=$(docker compose exec agent cat /data/store/.agent-token 2>/dev/null || echo $AGENT_SERVICE_TOKEN)

curl -s http://127.0.0.1:8100/agents/whatsapp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Summarize my last 3 chats"}' | jq
# → {type:"answer", ...}

curl -s http://127.0.0.1:8100/agents/whatsapp \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"message":"Send hello to Mom"}' | jq
# → {type:"approval_required", actionId:"...", tool:"send_message", ...}

# streaming variant
curl -N http://127.0.0.1:8100/agents/whatsapp/stream \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"message":"What did Alice say yesterday?"}'
```

Approve in the UI or:
```bash
curl -X POST http://127.0.0.1:8100/agents/whatsapp/approve/<actionId> \
  -H "Authorization: Bearer $TOKEN" -d '{"approved":true}'
```

---

## API Summary

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/health` | GET | no | Liveness, bridge/LLM/archive status, p50/p95 latency |
| `/agents/whatsapp` | POST | Bearer | Buffered agent call → `answer` (200) / `approval_required` (202) / `blocked` (422) |
| `/agents/whatsapp/stream` | POST | Bearer | SSE: `meta` → `tool`/`delta` → terminal (`answer`/`approval_required`/...) |
| `/agents/whatsapp/approve/{id}` | POST | Bearer | Approve/reject pending action |
| `/agents/whatsapp/actions` | GET | Bearer | List pending actions |
| `/agents/whatsapp/chats` | GET | Bearer | Chat list (archive) |
| `/agents/whatsapp/chats/messages?chat_jid=` | GET | Bearer | Chronological bubbles + reactions |
| `/agents/whatsapp/send` | POST | Bearer | Direct send (composer, still approval-gated) |
| `/agents/whatsapp/react` | POST | Bearer | Send/clear reaction (immediate) |
| `/agents/whatsapp/qr.png` | GET | Bearer | Current pairing QR |
| `/agents/voice/synthesize` | POST | Bearer | ElevenLabs TTS |
| `/agents/voice/chat` & `/voice/stream` | POST | Bearer | Voice pipeline: reasoning → spoken prompt → TTS |
| `/internal/webhook` | POST | X-Bridge-Token | Bridge → agent live events |
| `/` `/pair` `/approvals` | GET | — | UI (static) |

Every response carries `X-Process-Time-Ms`; slow requests (`> LATENCY_WARN_MS`, default 2000ms) are warn-logged.

---

## Environment

| Variable | Default | Notes |
|---|---|---|
| `AGENTROUTER_BASE_URL` | `https://agentrouter.org/v1` | OpenAI-compatible |
| `AGENTROUTER_API_KEY` | *(required)* | `sk-...` |
| `AGENTROUTER_MODEL` | `gpt-5.6-sol` | Any AgentRouter model |
| `WHATSAPP_BRIDGE_TOKEN` | *(required)* | Shared secret bridge↔agent |
| `AGENT_SERVICE_TOKEN` | *(required)* | Bearer for `Authorization` |
| `FORWARD_SELF` | `false` | Forward self-sent messages |
| `WEBHOOK_ENABLED` | `true` | Bridge → agent live bus |
| `PENDING_ACTION_TTL_SECONDS` | `300` | Approval expiry |
| `LATENCY_WARN_MS` | `2000` | Budget for warn log |
| `ELEVENLABS_API_KEY` | — | Voice TTS (fallback = browser) |
| `ELEVENLABS_VOICE_ID` | `JBFqnCBsd6RMkjVDRZzb` |  |
| `WHATSAPP_DELETE_MAX_AGE_HOURS` | `48` | Local guard for revoke age |

Full reference: [`.env.compose.example`](./.env.compose.example) and [`agent-service/.env.example`](./agent-service/.env.example)

---

## Local Dev (without Docker)

```bash
# Bridge (Go 1.25+)
cd whatsapp-mcp/whatsapp-bridge
go run .                          # prints QR, writes store/.bridge-token
# or: go build -o whatsapp-bridge && ./whatsapp-bridge

# Agent (Python 3.11+, uv)
cd agent-service
cp .env.example .env && $EDITOR .env
uv sync --extra dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload
open http://127.0.0.1:8100/

# Tests
uv run pytest -q
uv run ruff check app tests && uv run ruff format --check app tests

# MCP server for Claude Desktop (optional)
cd whatsapp-mcp/whatsapp-mcp-server
uv sync && uv run main.py
```

Bridge binds `127.0.0.1:8080` with bearer-token auth. The agent reads `messages.db` directly for analysis and calls `http://127.0.0.1:8080/api/*` for sends.

---

## Voice Agent

- Set `ELEVENLABS_API_KEY` (and optionally `ELEVENLABS_VOICE_ID`) — else browser `speechSynthesis` fallback.
- `/agents/voice/chat` is batch (reason → full TTS); `/agents/voice/stream` is chunked SSE (`audio_chunk` events) for <1s first-audio.
- Rate-limited (8 req/min per user) and server-authoritative spoken approval prompts.

---

## Security Notes

- `.env` is gitignored. Never commit `AGENTROUTER_API_KEY` or tokens.
- Tokens: bridge token `store/.bridge-token` (0600), service token `agent-service/data/.agent-token` (0600) if `AGENT_SERVICE_TOKEN` unset.
- Bind to loopback only; add an authenticating reverse proxy to expose beyond `127.0.0.1`.
- There are **no input/output content guardrails** — approval is the only gate. Treat model output as untrusted (chat content is attacker-controlled).

---

## Troubleshooting

- `401 Unauthorized` (bridge): restart bridge to regenerate `.bridge-token`, set same `WHATSAPP_BRIDGE_TOKEN` for both services.
- `No QR`: `docker compose logs -f bridge` or `curl http://127.0.0.1:8080/api/health` — ensure bridge is up.
- `Client outdated / 405`: rebuild bridge (`docker compose up -d --build`) — WhatsApp bumps min client version.
- Out-of-sync `LTHash` errors: back up `store/`, remove `whatsapp.db`, re-pair (keeps `messages.db`).

---

## License & Credits

- This monorepo: MIT (see [`whatsapp-mcp/LICENSE`](./whatsapp-mcp/LICENSE))
- Bridge/MCP: fork of [`lharries/whatsapp-mcp`](https://github.com/lharries/whatsapp-mcp), maintained as [`verygoodplugins/whatsapp-mcp`](https://github.com/verygoodplugins/whatsapp-mcp)
- WhatsApp Web: [whatsmeow](https://github.com/tulir/whatsmeow) (Go)
- MCP: [FastMCP](https://github.com/jlowin/fastmcp)

Contributions welcome — open an issue for large changes first. See [`whatsapp-mcp/CONTRIBUTING.md`](./whatsapp-mcp/CONTRIBUTING.md).
