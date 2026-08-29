# Security Audit Checklist

Operational checklist for reviewing this deployment before pilot/rollout.
Items marked **[gate]** must pass before any real WhatsApp account is linked.

## Secrets

- [ ] **[gate]** `AGENTROUTER_API_KEY` present only in `.env` / secret store — never in git, images, or logs. Verify: `git log --all --full-history -p | grep sk-` is empty.
- [ ] **[gate]** `WHATSAPP_BRIDGE_TOKEN` and `AGENT_SERVICE_TOKEN` are long random values (`openssl rand -hex 32`), not defaults from examples.
- [ ] `.bridge-token`, `data/.agent-token`, and `data/agent.db` are gitignored and excluded from images (`.dockerignore`).
- [ ] Secrets rotation procedure documented: rotate bridge token ⇒ restart both containers with new value; rotate service token ⇒ update router/frontend + approvals page.

## Network exposure

- [ ] **[gate]** Bridge port published only on loopback (`127.0.0.1:8080:8080`). The bridge additionally rejects non-loopback Host headers (defense in depth).
- [ ] Agent service published only on loopback (`127.0.0.1:8100:8100`) unless fronted by an authenticating reverse proxy with TLS.
- [ ] No `network_mode: host`; services talk over the internal compose network only.
- [ ] If exposed beyond localhost: TLS termination + real auth in front; consider mTLS or VPN/Wireguard for remote use.

## Authentication & authorization

- [ ] **[gate]** All agent-service routes except `/health` and `/approvals` (static HTML) require a valid bearer token — verify 401 without it.
- [ ] `X-User-ID` is trusted only because the caller holds the service token; document that token holders are trusted users.
- [ ] Bridge rejects unauthenticated `/api/*` calls (401) and unknown Host headers (403) — verify after deploy.

## Guardrails configuration

- [ ] **[gate]** Approval workflow enabled and tested end-to-end: propose → approve → execute; propose → reject → nothing sent.
- [ ] Decide `ALLOWED_RECIPIENTS`: empty allowlist means the agent can message *anyone*. For pilots, set an explicit allowlist.
- [ ] Review `BANNED_WORDS` policy for your user base.
- [ ] `BLOCK_URLS_IN_MESSAGES=true` unless there is a documented reason otherwise.
- [ ] Rate limits sized for the pilot group (`RATE_LIMIT_MUTATING_PER_HOUR` default 10).
- [ ] `WHATSAPP_DELETE_MAX_AGE_HOURS` set on the bridge (default suggestion 48h).

## Data handling

- [ ] Understand what is stored where: `messages.db` (full chat archive incl. media), `whatsapp.db` (session), `data/agent.db` (approvals + audit). All live in volumes — plan backups AND secure disposal.
- [ ] Volume permissions: named volumes initialize from image ownership (non-root UIDs 10000/10001). Verify no root-owned writable paths.
- [ ] Backups encrypted at rest if taken; session files (`whatsapp.db`) grant account access — treat as credentials.
- [ ] Retention policy decided: how long to keep `messages.db`, audit log, rate events.

## LLM-specific risks

- [ ] Prompt-injection guardrail active (score ≥ 2 blocks); review blocked-event samples weekly via `/audit`.
- [ ] Confirm chat content reaches the model only wrapped in `<untrusted_chat_data>`.
- [ ] Mutating tools unreachable by the LLM directly — code review: no execution path bypasses `pending_actions`.
- [ ] Understand residual risk: a determined injection can still influence *proposals*; the human approval step is the backstop. Train approvers to read args carefully.

## Operations

- [ ] Health monitoring wired: `/health` (service+bridge+archive status) polled by orchestrator or uptime tool.
- [ ] Log retention + shipping decided (`docker compose logs` at minimum).
- [ ] Incident response: unlink device (WhatsApp → Linked Devices), stop containers, rotate tokens, inspect audit trail.
- [ ] Update process: rebuild images on upstream repo updates; re-pairing NOT required for updates (session persists in volume).
