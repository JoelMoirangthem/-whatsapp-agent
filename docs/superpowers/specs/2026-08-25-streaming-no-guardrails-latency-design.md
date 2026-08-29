# Design: Streaming, No-Guardrails, Sub-2s WhatsApp Agent

Date: 2026-08-25 · Status: approved in chat

## Decisions (user-approved)

1. **Guardrail module only**: delete `app/guardrails/` (input sanitization, injection
   scoring, banned words, URL policy, recipient allowlist, rate limits). The human
   **approval gate stays** for send/delete. Audit log stays as its record.
2. **Latency budget = fast start + streamed end**: first visible output <2s via
   streaming; every non-LLM endpoint hard-measured against a 2000ms warn threshold;
   LLM answers stream until the model finishes (no truncation).
3. **Approach A**: keep LangChain, switch to native `.stream()`; new SSE endpoint;
   kill the malformed-JSON repair round-trip from the common path.

## Changes

### Removals
- `app/guardrails/` deleted; all imports/usages in `agent.py` removed.
- Settings dropped: `RATE_LIMIT_*`, `BANNED_WORDS`, `ALLOWED_RECIPIENTS`,
  `BLOCK_URLS_IN_MESSAGES`, `MAX_INPUT_CHARS`, `MAX_MESSAGE_CHARS`.
- `db.py`: `rate_events` schema + counter methods deleted.
- Compose/env examples/README cleaned to match reality.
- Kept (correctness, not safety): delete-target ownership pre-check (WhatsApp
  physically rejects revoking others' messages), tool-result context caps that
  bound prompt size, accuracy rules in the system prompt.

### Streaming
- `POST /agents/whatsapp/stream` → `text/event-stream`.
  Events: `meta{requestId}` → `tool{name}` per read-only round → `delta{text}`
  answer tokens → terminal `answer|proposal|blocked|error` carrying full payload +
  `processTimeMs`.
- Protocol: streamed buffer starting with `{` is parsed as the existing JSON
  tool-call/proposal contract; anything else is a plain-text answer piped live.
  Answers therefore bypass JSON entirely — the old repair round-trip
  (`parse_llm_message`) disappears from the common case; worst case remains one
  corrective re-ask for tool-call JSON only.
- Legacy buffered `POST /agents/whatsapp` unchanged externally (same pipeline,
  no delta callbacks).

### Latency instrumentation & budgets
- Middleware on every response: `X-Process-Time-Ms` header + structured log;
  warn above `LATENCY_WARN_MS` (default 2000). Rolling p50/p95 exposed in `/health`.
- Per-stage timings (`first_token_ms`, `llm_ms`, `bridge_exec_ms`) into audit.
- Fail-fast tuning: `ChatOpenAI(max_retries=0)`; httpx timeouts connect≈2s /
  read `LLM_TIMEOUT_SECONDS` (45→30 default); bridge client cap 10s→5s;
  `MessageArchive` reuses one cached read-only SQLite connection instead of
  connecting per query.

### UI
Copilot sends via `fetch` + ReadableStream against the stream endpoint: live token
rendering, tool-status chips, per-response latency badge, inline Approve/Reject as
today, automatic fallback to the legacy endpoint if the stream fails.

### Tests
Delete `test_guardrails_input.py`, `test_guardrails_output.py`, `test_ratelimit.py`;
drop guardrail-specific cases from `test_api_flows.py`; `FakeLLM` gains `astream`;
new `test_stream.py`: SSE sequencing, JSON-vs-text mode detection, latency headers.
Gate: `uv run pytest -q` green, `ruff check` clean.
