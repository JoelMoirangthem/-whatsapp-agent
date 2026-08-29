# WhatsApp Agent Service

Approval-gated WhatsApp agent. Orchestrates an LLM (via [AgentRouter](https://agentrouter.org)) with **read-only** tools (analyze chats) and **mutating** tools (send / delete messages) on top of the [whatsapp-mcp Go bridge](../whatsapp-mcp/).

**Core safety contract:** mutating actions are *never* executed directly by the LLM. Every send/delete becomes a pending action that requires explicit human approval before the bridge is called. There is no other content filtering — requests pass through unmodified apart from that gate.

## Architecture

```
User ──► POST /agents/whatsapp[/stream]
                │
             LLM (AgentRouter, streamed)
                │
     ┌──────────┴───────────┐
     ▼                      ▼
 read-only tools       mutating tools
 (list_chats,          (send_message, delete_message)
  get_messages,              │
  search_chats)              ▼
     │                 pending action (SQLite, TTL 5 min)
     ▼                       │
   answer              POST /agents/whatsapp/approve/{id}
 (streamed)                  │
                             ▼
                    Go bridge REST ──► WhatsApp
```

Reads come from the bridge's local `messages.db` SQLite archive directly; writes go through the bridge's bearer-token REST API.

## Streaming

`POST /agents/whatsapp/stream` returns `text/event-stream`:

| Event | Meaning |
|---|---|
| `meta` | `{requestId, model}` |
| `tool` | read-only tool about to run (`{tool, args}`) |
| `delta` | answer tokens as the model generates them |
| `answer` / `approval_required` / `blocked` / `error` | terminal; full payload + `processTimeMs` |

Plain-text answers stream token-by-token and never touch JSON parsing; tool-call proposals use the JSON contract and are buffered. The legacy buffered endpoint `POST /agents/whatsapp` is unchanged and remains fully supported.

## Latency

- Every response carries `X-Process-Time-Ms`; requests above `LATENCY_WARN_MS` (default 2000) are logged.
- `/health` reports rolling p50/p95 across recent requests plus per-stage timings (`first_token_ms`, `llm_ms`, `bridge_exec_ms`) in the audit trail.
- Fail-fast tuning: zero LLM retries, ~2s connect timeout, streamed reads.

## API

### `GET /health`
Liveness probe: LLM config, bridge reachability, archive availability, latency percentiles.

### `POST /agents/whatsapp`
Body: `{"message": "..."}` · Header: `Authorization: Bearer <token>`, optional `X-User-ID`.

| `type` | HTTP | Meaning |
|---|---|---|
| `answer` | 200 | Direct response to a read-only request |
| `approval_required` | 202 | Mutating action proposed; includes `actionId`, `tool`, `args`, `warnings`, `expiresAt` |
| `blocked` | 422 | Refused pre-execution (e.g. delete target not owned by this account) |
| `error` | 503 | LLM unavailable / unparseable output |

### Approval lifecycle
`pending → approved → executed | failed` · `pending → rejected` · `pending → expired`

Actions auto-expire after `PENDING_ACTION_TTL_SECONDS` (default 300 s). Deciding an already-decided action returns `409`; an expired one returns `410`. Every transition is written to the audit trail.

### Other endpoints
`GET /audit?limit=100`, `GET /agents/whatsapp/actions[/{id}]`, `GET /agents/whatsapp/chats`, `GET /agents/whatsapp/chats/messages?chat_jid=`, `POST /agents/whatsapp/send` (composer send without LLM), `GET /agents/whatsapp/qr/meta`, `GET /agents/whatsapp/qr.png`.

On first start a service token is generated to `data/.agent-token` (mode 0600). All API calls require `Authorization: Bearer <token>`; local browsers fetch it automatically from loopback-only `/pair-token`.

## Tools exposed to the LLM

| Tool | Kind | Notes |
|---|---|---|
| `list_chats` | read-only | recent chats from local archive |
| `get_messages` | read-only | chat transcript for analysis |
| `search_chats` | read-only | name/JID substring search |
| `send_message` | **approval required** | no content checks — approval is the only gate |
| `delete_message` | **approval required** | pre-checked: target must exist locally and be sent by this account (WhatsApp rejects revoking others' messages) |

## Security notes

- `.env` (contains `AGENTROUTER_API_KEY`) is gitignored — never commit it.
- Service token generated 0600 to `data/.agent-token` unless `AGENT_SERVICE_TOKEN` set; bridge token auto-read from `whatsapp-bridge/store/.bridge-token`.
- Bind to loopback only. If exposing beyond localhost, put an authenticating reverse proxy in front.
- **There are no input/output content guardrails anymore.** Anyone holding the service token can propose sends to anyone; the only protection is that nothing mutates WhatsApp state until you click Approve. Chat content fed to the model is attacker-controlled (anyone who can message the account); treat model output accordingly.

## Quick start

```bash
cd agent-service
cp .env.example .env && $EDITOR .env   # AgentRouter key
uv sync --extra dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8100
open http://127.0.0.1:8100/
```

## Configuration reference

See [.env.example](./.env.example) — every variable is documented there.

## Development

```bash
uv sync --extra dev
uv run pytest -q          # 26 tests, offline & deterministic
uv run ruff check app tests
uv run ruff format app tests
```

Tests use scripted fake streaming LLMs, a fake bridge, and temp SQLite archives.

## Status / roadmap

- [x] Bridge `DELETE /api/messages` endpoint (see ../whatsapp-mcp)
- [x] Agent service with tools + LLM orchestration
- [x] Approval workflow + audit trail
- [x] SSE token streaming + latency instrumentation
- [x] Docker packaging (root `docker-compose.yml`)
- [ ] End-to-end tests against a paired WhatsApp account
- [ ] Monitoring/alerting beyond the latency log + `/health` percentiles
