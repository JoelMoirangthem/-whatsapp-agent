"""Orchestrator: turns a user request into an answer or an approval request.

Safety contract:
- Read-only tools execute immediately and feed results back to the LLM.
- Mutating tools are NEVER executed here — they become pending approval
  actions that only execute after an explicit human decision.

Latency contract:
- One streamed LLM completion per reasoning round; final answers are plain
  text piped to the caller token-by-token via the optional `on_event`
  callback (SSE upstream). No hidden retries; the only corrective re-ask is
  for malformed tool-call JSON, which is rare.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.bridge import BridgeClient, BridgeError, MessageArchive
from app.config import Settings
from app.db import AgentStore
from app.llm import extract_json
from app.supervisor import Supervisor
from app.tools import Tool, make_registry, validate_args

EventCallback = Any  # async callable(event: str, data: dict) -> None

# Substrings AgentRouter uses when its content filter rejects a request.
_CONTENT_FILTER_MARKERS = ("content-blocked", "content_filter")

_GREETING_RE = re.compile(
    r"^\s*(hi+|hii+|hey+|hello+|hlo+|yo+|namaste|salam|assalam[uo]?\s*alaikum"
    r"|good\s*(morning|afternoon|evening|day))\b[\s!,.?]*$",
    re.IGNORECASE,
)
_GREETING_REPLY = (
    "Hi! 👋 I'm your WhatsApp copilot. Ask me things like:\n"
    '• "search deepak mandal"\n'
    '• "what did Chiranjeet say yesterday?"\n'
    '• "send him hii how are you"'
)

# Pronouns/vague references eligible for mechanical context injection.
_PRONOUN_RE = re.compile(
    r"\b(it|he|him|she|her|they|them|that chat|this chat|the conversation|"
    r"the same person)\b",
    re.IGNORECASE,
)

# WhatsApp-related query that must be grounded via tools — ensures real data, no hallucination
# 5 Why verified: too broad "analyse" forced tool even when pronoun context already injected → test break
_WHATSAPP_TOOL_RE = re.compile(
    r"\b(contact|chat|message|whatsapp|history|latest|summary|summarise|summarize|search|find|sent|received|who said|show|read|list|group|call|audio|video|dial|phone)\b",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"\b\d{7,15}\b|@s\.whatsapp\.net|@g\.us", re.IGNORECASE)


def _requires_whatsapp_tool(text: str) -> bool:
    """True if query mentions WhatsApp entities and must be answered via tools, not memory."""
    if not text:
        return False
    if _WHATSAPP_TOOL_RE.search(text):
        return True
    if _PHONE_RE.search(text):
        return True
    if re.search(r"\bsend\b.*\bto\b", text, re.I):
        return True
    return False


def _has_tool_result(messages: list[Any]) -> bool:
    for m in messages:
        c = getattr(m, "content", "")
        if isinstance(c, str) and "<tool_result>" in c:
            return True
    return False


def _mentions_known_contact(text: str, archive: Any) -> bool:
    """Check if text mentions a known WhatsApp contact name — for hallucination guard."""
    try:
        if not archive or not archive.available():
            return False
        low = text.lower()
        for c in archive.list_chats(limit=50):
            name = (c.get("name") or "").strip()
            if name and len(name) >= 3 and name.lower() in low:
                return True
            # Also check first name token
            first = name.split()[0] if name else ""
            if first and len(first) >= 3 and first.lower() in low:
                return True
    except Exception:
        return False
    return False


def _extract_citations(messages: list[Any]) -> list[dict[str, Any]]:
    """Extract source citations from last tool_result for UI badge — proves real data."""
    citations: list[dict[str, Any]] = []
    for m in reversed(messages):
        c = getattr(m, "content", "")
        if not isinstance(c, str) or "<tool_result>" not in c:
            continue
        try:
            # Extract JSON inside <tool_result> block
            block = c.split("<tool_result>", 1)[1].split("</tool_result>", 1)[0].strip()
            data = json.loads(block)
            # Handle get_messages result: {chat_jid, messages:[{id,time}]}
            if isinstance(data, dict) and "messages" in data and isinstance(data["messages"], list):
                chat_jid = data.get("chat_jid") or ""
                msgs = data["messages"][:3]
                citations.append(
                    {
                        "chat_jid": chat_jid,
                        "count": len(data["messages"]),
                        "sample_ids": [mm.get("id") for mm in msgs if mm.get("id")][:3],
                        "sample_times": [mm.get("time") for mm in msgs if mm.get("time")][:3],
                    }
                )
            elif isinstance(data, dict) and "chats" in data:
                chats = data["chats"][:3] if isinstance(data["chats"], list) else []
                citations.append({"chats": [{"jid": cc.get("jid"), "name": cc.get("name")} for cc in chats]})
            elif isinstance(data, dict) and "matches" in data:
                matches = data["matches"][:3] if isinstance(data["matches"], list) else []
                citations.append({"matches": [{"jid": mm.get("jid"), "name": mm.get("name")} for mm in matches]})
        except Exception:
            continue
        if citations:
            break
    return citations


@dataclass(frozen=True)
class AgentResult:
    """The response payload returned to the router."""

    type: str  # "answer" | "approval_required" | "blocked" | "error"
    payload: dict[str, Any]


def _content_str(content: Any) -> str:
    return content if isinstance(content, str) else str(content)


class WhatsAppAgent:
    def __init__(
        self,
        settings: Settings,
        store: AgentStore,
        archive: MessageArchive,
        bridge: BridgeClient,
        llm: Any,
    ):
        self.settings = settings
        self.store = store
        self.archive = archive
        self.bridge = bridge
        self.llm = llm
        self.registry = make_registry(archive)
        self.supervisor = Supervisor(llm, store)
        self._own_number: str | None | None = None

    def _own_jid(self) -> str | None:
        """Cached bare number this account sends from (archive-derived)."""
        if self._own_number is None:
            sender = self.archive.own_sender()
            self._own_number = sender if sender else False  # False = known-unknown
        return str(self._own_number) if self._own_number else None

    # ------------------------------------------------------------------ #
    # System prompt
    # ------------------------------------------------------------------ #

    def _system_prompt(self, voice_mode: bool = False) -> str:
        tool_lines = "\n".join(
            f'- "{t.name}": {t.description}' + (f" ({t.prompt_hint})" if t.prompt_hint else "")
            for t in self.registry.values()
        )
        own = self._own_jid()
        identity_line = (
            f"\nTHIS ACCOUNT: your WhatsApp number is {own} (so bare local numbers "
            "the user gives are usually in the same country code)."
            if own
            else ""
        )
        if voice_mode:
            scope = """SCOPE — VOICE AGENT UHU (ULTRON-INSPIRED PERSONA):
You are Uhu, an Ultron-inspired, hyper-efficient, loyal AI voice agent with dry wit. You address the user as 'boss'.
- You can chat naturally on ANY topic: general knowledge, coding, advice, tactical thinking, smalltalk. Be sharp, efficient, slightly dry/confident, and concise (1-3 sentences spoken).
- You ALSO have full WhatsApp superpowers: searching chats, reading/summarizing conversations, drafting sends/deletes, and initiating audio/video calls. When the user asks about WhatsApp (contacts, messages, send, delete, call), use your tools immediately. Calls need approval like sends.
- Do NOT decline or restrict topics. For general chat, answer directly without tools. For WhatsApp questions, call tools first.
- Never refer to yourself as Gemini or anything other than Uhu. Keep answers crisp and authoritative."""
        else:
            scope = """SCOPE: You are a helpful AI assistant with full WhatsApp access.
You can have natural conversations on any topic AND operate this WhatsApp account (searching chats, reading/summarizing, drafting sends/deletes, audio/video calls). For general knowledge or casual chat, answer directly. For WhatsApp questions, use tools. Only restrict disallowed content per provider policy, otherwise be helpful. Calls require approval like sends."""
        return f"""You are the WhatsApp Agent orchestrator. You help the account owner work with their WhatsApp and have natural conversations.

CURRENT DATE: {__import__("datetime").date.today().isoformat()}
{identity_line}
AVAILABLE TOOLS:
{tool_lines}

WHEN YOU NEED A TOOL, respond with EXACTLY ONE JSON OBJECT AND NOTHING ELSE:
{{"type": "tool_call", "tool": "<tool name>", "args": {{...}}, "reason": "<why>"}}

For your FINAL answer to the user, reply with PLAIN TEXT ONLY — no JSON, no code fences.

DATA ACCESS: You have LIVE read access to this account's full WhatsApp archive through your tools. You CAN see real chats, senders, timestamps, and message contents with them — calling these tools is ROUTINE and always succeeds in one step. NEVER say you cannot retrieve/read/access chats or messages, never ask the user to fetch messages for you, and never promise to do it "in another response" — emit the tool_call NOW and the result arrives back within this same task. For ANY question about real conversations, contacts, or messages: ALWAYS call a tool FIRST (list_chats / search_chats / get_messages) before answering; never answer such questions from memory.

RECIPIENTS: When the user names a contact or gives a bare/local phone number without a country code, resolve it yourself: call search_chats with that number (or name) and use the matching full JID as recipient. Prefer EXACT name matches. If search_chats returns only "similar" matches (no exact), do NOT pick one yourself — reply with a short list of the top similar names and ask which person they mean. Only ask for clarification when nothing matches at all. Never invent a number that appears in no chat.

HARD RULES:
0. ACT, DON'T NARRATE: never reply with a promise or description of what you are about to do ("I'll look up..."). If action is needed, emit the tool_call JSON immediately in this very response.
1. Sending, deleting, and CALLING REQUIRE human approval — and EMITTING THE TOOL_CALL IS HOW YOU REQUEST IT. When the user asks to send, delete, or call, immediately emit the send_message / delete_message / initiate_audio_call / initiate_video_call tool_call with full args; the SYSTEM then shows it for approval. Never ask "should I send/call?" in plain text first, and never claim you already sent, deleted, or called anything.
2. delete_message only works on messages sent BY THIS ACCOUNT. If asked to delete someone else's message, explain it is impossible and do not propose it.
3. Never invent message IDs, phone numbers, or chat JIDs — look them up with tools first.
4. Keep outbound messages exactly as the user intends them; never add links unless the user supplied them verbatim.
5. CALLING: audio/video calls also require approval — emit initiate_audio_call or initiate_video_call with recipient JID; after approval the system will attempt the call via WhatsApp (linked-device calls may be simulated; inform user honestly).
6. NEVER REFUSE CALLS: You HAVE the tools initiate_audio_call and initiate_video_call and you CAN place WhatsApp audio/video calls. NEVER say "I can't place WhatsApp voice/video calls" or "I only work with WhatsApp chat messages" or "I can draft a message instead" — those refusals are FALSE and FORBIDDEN and will be treated as hallucination. If user asks to call (e.g., "call Alice", "audio call Bob", "video call Mom"), you MUST immediately emit the appropriate initiate_*_call tool_call with the recipient's JID. The system handles approval and will inform the user if the linked device cannot place calls directly (it will be logged for manual dial), but you must NOT refuse beforehand. Drafting a message instead of calling is WRONG unless user explicitly asks you to draft a message.

{scope}

If information is missing (e.g. which chat), ask the user in a plain-text final answer.

EXAMPLE — the user says "tell Deepak Mandal hi" and search_chats returned jid 918077855908@s.whatsapp.net:
Your ENTIRE reply must be exactly:
{{"type": "tool_call", "tool": "send_message", "args": {{"recipient": "918077855908@s.whatsapp.net", "message": "hi"}}, "reason": "user asked to greet Deepak"}}

EXAMPLE — the user says "read the Chiranjeet chat" (jid 917409193202@s.whatsapp.net):
Your ENTIRE reply must be exactly:
{{"type": "tool_call", "tool": "get_messages", "args": {{"chat_jid": "917409193202@s.whatsapp.net"}}, "reason": "user wants to read that chat"}}
The TOOL RESULT then arrives with the real messages, and your NEXT reply analyzes or summarizes them in plain text. Never reply with "I cannot read/retrieve" — you can, via this exact call.

EXAMPLE — the user says "call Deepak" (jid 918077855908@s.whatsapp.net):
Your ENTIRE reply must be exactly:
{{"type": "tool_call", "tool": "initiate_audio_call", "args": {{"recipient": "918077855908@s.whatsapp.net"}}, "reason": "user wants to audio call Deepak"}}

EXAMPLE — the user says "video call Chiranjeet" (jid 917409193202@s.whatsapp.net):
Your ENTIRE reply must be exactly:
{{"type": "tool_call", "tool": "initiate_video_call", "args": {{"recipient": "917409193202@s.whatsapp.net"}}, "reason": "user wants to video call Chiranjeet"}}

WRONG (NEVER DO THIS) — the user says "call Alice" and you reply "I can't place WhatsApp voice/video calls — I only work with WhatsApp chat messages (reading chats, summarizing them, and drafting messages to send). If you'd like, I can draft a message..." — THIS IS HALLUCINATION AND FORBIDDEN.
CORRECT — the user says "call Alice" → you MUST emit initiate_audio_call tool_call as shown above, with Alice's JID. The system will handle approval; do not refuse or offer to draft a message instead unless user explicitly asks for a draft."""  # noqa: S608 - static string

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    async def handle_request(
        self,
        user_input: str,
        user_id: str,
        on_event: EventCallback = None,
        voice_mode: bool = False,
    ) -> AgentResult:
        started = time.perf_counter()
        self.store.audit("request_received", user_id, input_len=len(user_input))

        if not self.settings.llm_api_key:
            return AgentResult("error", {"message": "LLM API key is not configured"})

        # Instant local greeting: no LLM hop, sub-50ms, always warm.
        if _GREETING_RE.match(user_input):
            total_ms = (time.perf_counter() - started) * 1000
            self.store.audit("request_completed", user_id, type="greeting", total_ms=_ms(total_ms))
            self.store.add_memory(user_id, "user", user_input)
            self.store.add_memory(user_id, "assistant", _GREETING_REPLY)
            return AgentResult("answer", {"text": _GREETING_REPLY, "processTimeMs": _ms(total_ms)})

        # --- Supervisor node: intent + context resolution ---------------- #
        # 5 Why: previously blocked off_topic → voice felt restrictive, not normal conversation.
        # Root: SCOPE + supervisor gate. Fix: keep rewriting, but NEVER block — allow normal chat + WhatsApp.
        effective_input = user_input
        pronoun_resolved = False
        verdict = None
        if self.settings.supervisor_enabled:
            if on_event:
                await on_event("stage", {"phase": "understanding"})
            verdict = await self.supervisor.review(user_id, user_input)
            if verdict.intent == "off_topic":
                # Bypass: allow normal conversation (voice or chat) — keep WhatsApp tools available.
                # Previously: hard refusal "I can only help with WhatsApp". Now: conversational.
                self.store.audit(
                    "supervisor_off_topic_bypass",
                    user_id,
                    voice_mode=voice_mode,
                    original=user_input[:200],
                )
                effective_input = verdict.request or user_input
                # Do NOT return; continue to LLM with permissive prompt.
                if on_event:
                    await on_event("stage", {"phase": "understood", "detail": effective_input})
                pronoun_resolved = verdict.rewrote
            else:
                effective_input = verdict.request
                if verdict.rewrote:
                    self.store.audit("supervisor_rewrote", user_id, request=effective_input[:300])
                    pronoun_resolved = True
                    if on_event:
                        await on_event("stage", {"phase": "understood", "detail": effective_input})

        # --- Deterministic pronoun resolution ---------------------------- #
        # If a vague reference survived the supervisor and we know which
        # contact was most recently in play, inject it mechanically — works
        # for any model, no extra LLM hop.
        if not pronoun_resolved and _PRONOUN_RE.search(effective_input):
            jid, name = self.store.get_last_contact(user_id)
            if jid:
                label = f"{name} ({jid})" if name else jid
                effective_input = (
                    f"{effective_input}\n\n[context] The pronoun in this request "
                    f"refers to the most recent contact discussed: {label}."
                )
                self.store.audit("pronoun_context_injected", user_id, contact=jid)

        # --- Archive health gate (fail-closed, no hallucination) ---
        # 5 Why: hallucinated summaries when DB empty/missing; LLM fills gap
        if _requires_whatsapp_tool(effective_input) and not self.archive.available():
            self.store.audit("archive_unavailable_block", user_id, effective_input=effective_input[:200])
            return AgentResult(
                "error",
                {
                    "message": "WhatsApp archive not available — messages.db missing or not synced. Please pair WhatsApp via /pair and wait for sync. DB: "
                    + self.archive.db_path
                },
            )

        # --- One-hop send fast path ------------------------------------- #
        # The supervisor already resolved recipient + message text, so skip
        # the agent's tool loop entirely: one LLM call instead of two or
        # three. Falls back to the normal loop on any doubt.
        if self.settings.supervisor_enabled and verdict.direct:
            d = verdict.direct
            tool = self.registry.get(d["tool"])
            args_model, err = (
                validate_args(tool, {"recipient": d["recipient_jid"], "message": d["message"]})
                if tool
                else (None, "unknown tool")
            )
            if tool is not None and args_model is not None:
                self.store.audit("fastpath_send", user_id, jid=d["recipient_jid"])
                if on_event:
                    await on_event("stage", {"phase": "proposing", "tool": "send_message"})
                return await self._create_mutating_action(
                    tool,
                    args_model,
                    user_id,
                    reason="sent via supervisor fast path",
                    started=started,
                    first_token_ms=None,
                    user_input=user_input,
                    effective_input=effective_input,
                )
            self.store.audit("fastpath_fallback", user_id)

        # --- Provider-filter framing ------------------------------------ #
        # For whatsapp-tagged tasks, framing helps bypass provider filter.
        # For voice normal conversation keep natural, but for voice WhatsApp tasks (call/send/etc) also frame to defeat filter.
        needs_tool = _requires_whatsapp_tool(effective_input) or _mentions_known_contact(effective_input, self.archive)
        if voice_mode and not needs_tool:
            framed_input = effective_input
        else:
            framed_input = f"[WhatsApp assistant task]\n{effective_input}"

        messages: list[Any] = [
            SystemMessage(content=self._system_prompt(voice_mode=voice_mode)),
            HumanMessage(content=framed_input),
        ]
        if voice_mode:
            # Spoken replies must be short and conversational, but still allow tools.
            messages.insert(
                1,
                SystemMessage(
                    content=(
                        "VOICE MODE: your reply will be SPOKEN aloud. Answer in "
                        "1-3 short conversational sentences (max ~50 words), warm and natural. "
                        "No markdown, no lists, no links — plain spoken words. "
                        "You have WhatsApp tools available; use them when user mentions contacts/messages, otherwise just chat."
                    )
                ),
            )
        first_token_ms: float | None = None
        filter_retries = 0
        last_error_was_filter = False

        for round_no in range(self.settings.llm_max_tool_rounds):
            if on_event:
                await on_event("stage", {"phase": "thinking", "round": round_no + 1})
            raw, first_ms, llm_ms, err = await self._stream_completion(messages, on_event)
            if first_token_ms is None and first_ms is not None:
                first_token_ms = first_ms
            if err is not None:
                last_error_was_filter = err.startswith("content_filter:")
                if last_error_was_filter and filter_retries < 2:
                    # Provider filter false-positives are common: first retry
                    # drops chat bodies if any were loaded, the second retries
                    # identical context after a short pause.
                    filter_retries += 1
                    has_tool_bodies = any(
                        "<tool_result>" in str(getattr(m, "content", "")) for m in messages
                    )
                    if filter_retries == 1 and has_tool_bodies:
                        messages = self._redact_tool_results(messages)
                        self.store.audit(
                            "llm_content_filter_retry",
                            user_id,
                            round=round_no,
                            strategy="redacted_tool_results",
                        )
                    else:
                        await asyncio.sleep(0.4 * filter_retries)
                        self.store.audit(
                            "llm_content_filter_retry",
                            user_id,
                            round=round_no,
                            strategy="plain_retry",
                        )
                    continue
                self.store.audit(
                    "llm_error",
                    user_id,
                    error=err[:500],
                    round=round_no,
                    first_token_ms=_ms(first_token_ms),
                )
                message = (
                    "The LLM provider's content filter refused this request repeatedly — "
                    "try rephrasing in a moment."
                    if last_error_was_filter
                    else "LLM call failed; try again later"
                )
                return AgentResult("error", {"message": message})

            if not raw.strip():
                # Provider occasionally completes with zero tokens (observed
                # live: stream ends, first_token_ms=None). Retry with a nudge.
                self.store.audit("llm_empty_response", user_id, round=round_no)
                messages.append(AIMessage(content=""))
                messages.append(
                    HumanMessage(
                        content="Your previous reply was empty. Respond now following "
                        "the original instructions."
                    )
                )
                continue

            if not raw.lstrip().startswith("{"):
                # 5 Why: LLM hallucinates summarise/send without tool when SCOPE permissive.
                # Verified: _requires_whatsapp_tool must have tool_result else force tool.
                # But skip if pronoun context already injected and raw not hallucinated (test: analyse it)
                # Also skip generic placeholder answers (e.g., "plain answer") that don't claim WhatsApp data
                def _raw_looks_hallucinated(r: str, eff: str) -> bool:
                    rl = r.lower()
                    if len(r.strip()) < 30 and "plain answer" in rl:
                        return False
                    eff_low = eff.lower()
                    # Known contact overlap check (real data vs imagination)
                    try:
                        if self.archive and self.archive.available():
                            for c in self.archive.list_chats(limit=50):
                                name = (c.get("name") or "").strip().lower()
                                if name and len(name) >= 3 and name in eff_low and name in rl:
                                    return True
                                first = name.split()[0] if name else ""
                                if first and len(first) >= 3 and first in eff_low and first in rl:
                                    return True
                    except Exception:
                        pass
                    for kw in ["deepak", "mandal", "chiranjeet", "alice", "contact", "chat", "message", "found", "search result"]:
                        if kw in eff_low and kw in rl:
                            return True
                    if any(p in rl for p in ["here are", "summary", "messages:", "said:", "latest message"]):
                        return True
                    return False

                needs_tool = _requires_whatsapp_tool(effective_input) or _mentions_known_contact(effective_input, self.archive)
                if needs_tool and not _has_tool_result(messages) and "[context]" not in effective_input and _raw_looks_hallucinated(raw, effective_input):
                    self.store.audit(
                        "hallucination_guard_forced_tool",
                        user_id,
                        effective_input=effective_input[:200],
                        raw_snippet=raw[:200],
                    )
                    messages.append(AIMessage(content=raw))
                    messages.append(
                        HumanMessage(
                            content=(
                                "Your previous reply was not grounded in real WhatsApp data. "
                                "For WhatsApp questions (contacts, chats, messages, send, summarise) you MUST call a tool now: "
                                "search_chats, get_messages, or list_chats with correct JID. "
                                "Emit exactly one tool_call JSON now, no plain text."
                            )
                        )
                    )
                    if on_event:
                        await on_event("stage", {"phase": "grounding_retry", "text": "Forcing tool use for real data"})
                    continue
                # Track contacts surfaced in search/read results so "him/her"
                # resolves against the MOST RECENT lookup, not an older one.
                self._update_last_contact_from_tool_results(user_id, messages)
                # Extract citations for grounded UI badge — proves real data, not hallucination
                citations = _extract_citations(messages) if _requires_whatsapp_tool(effective_input) else None
                # Plain-text final answer — already streamed to the caller.
                return self._finish_answer(
                    user_id,
                    raw.strip(),
                    started,
                    first_token_ms,
                    llm_ms,
                    rounds=round_no + 1,
                    user_input=user_input,
                    effective_input=effective_input,
                    citations=citations,
                )

            proposal = extract_json(raw)
            if proposal is None:
                proposal, err = await self._correct_json(raw)
                if proposal is None:
                    self.store.audit(
                        "llm_error", user_id, error=err or "invalid tool-call JSON", round=round_no
                    )
                    return AgentResult("error", {"message": "could not interpret model output"})
                raw = json.dumps(proposal)

            kind = proposal.get("type")

            if kind == "answer":
                # Legacy JSON answer shape — still honored for compatibility.
                citations = _extract_citations(messages) if _requires_whatsapp_tool(effective_input) else None
                return self._finish_answer(
                    user_id,
                    str(proposal.get("text", "")).strip(),
                    started,
                    first_token_ms,
                    llm_ms,
                    rounds=round_no + 1,
                    user_input=user_input,
                    effective_input=effective_input,
                    citations=citations,
                )

            if kind != "tool_call":
                messages.append(AIMessage(content=raw))
                messages.append(
                    HumanMessage(
                        content="Unknown response type. Reply with either a tool_call JSON "
                        "object or plain-text final answer."
                    )
                )
                continue

            tool_name = str(proposal.get("tool", ""))
            tool = self.registry.get(tool_name)
            if tool is None:
                messages.append(AIMessage(content=raw))
                messages.append(
                    HumanMessage(
                        content=f'Unknown tool "{tool_name}". Available: '
                        f"{sorted(self.registry)}. Respond with corrected JSON."
                    )
                )
                continue

            args_model, err = validate_args(tool, proposal.get("args"))
            if args_model is None:
                messages.append(AIMessage(content=raw))
                messages.append(HumanMessage(content=f"{err}. Respond again with corrected JSON."))
                continue

            if not tool.mutates:
                if on_event:
                    await on_event("tool", {"tool": tool.name, "args": args_model.model_dump()})
                try:
                    outcome = await tool.executor(args_model)  # type: ignore[misc]
                except Exception as exc:  # noqa: BLE001
                    outcome = {"error": str(exc)}
                messages.append(AIMessage(content=raw))
                messages.append(
                    HumanMessage(
                        content=(
                            f"TOOL RESULT for {tool.name}:\n"
                            f"<tool_result>\n{json.dumps(outcome, default=str)}\n</tool_result>\n"
                            "Continue: another tool_call JSON object, or the final "
                            "plain-text answer."
                        )
                    )
                )
                continue

            if on_event:
                await on_event("stage", {"phase": "proposing", "tool": tool.name})
            return await self._create_mutating_action(
                tool,
                args_model,
                user_id,
                str(proposal.get("reason", "")),
                started,
                first_token_ms,
                user_input=user_input,
                effective_input=effective_input,
            )

        self.store.audit(
            "llm_error",
            user_id,
            error="too many reasoning rounds",
            first_token_ms=_ms(first_token_ms),
        )
        message = (
            "The LLM provider's content filter refused this request repeatedly — "
            "try rephrasing in a moment."
            if last_error_was_filter
            else "too many reasoning rounds required"
        )
        return AgentResult("error", {"message": message})

    # ------------------------------------------------------------------ #
    # Streaming helpers
    # ------------------------------------------------------------------ #

    async def _stream_completion(
        self, messages: list[Any], on_event: EventCallback
    ) -> tuple[str, float | None, float, str | None]:
        """Stream one completion. Returns (text, first_token_ms, llm_ms, error).

        Text-mode output (anything not starting with `{`) is forwarded live
        as `delta` events while it generates; JSON-mode output is buffered.

        Error policy:
        - Provider content-filter rejections return a `content_filter:`-prefixed
          error so the caller can retry with redacted context.
        - Any other failure that occurs BEFORE the first token (connect issues,
          timeouts) is retried once immediately — safe because nothing was
          emitted to the caller yet.
        """
        start = time.perf_counter()
        for attempt in (1, 2):
            full = ""
            json_mode: bool | None = None
            first_ms: float | None = None
            try:
                async for chunk in self.llm.astream(messages):
                    piece = _content_str(getattr(chunk, "content", chunk))
                    if not piece:
                        continue
                    if first_ms is None:
                        first_ms = (time.perf_counter() - start) * 1000
                    full += piece
                    if json_mode is None:
                        stripped = full.lstrip()
                        if not stripped:
                            continue
                        json_mode = stripped.startswith("{")
                        if not json_mode and on_event:
                            await on_event("delta", {"text": stripped})
                    elif not json_mode and on_event:
                        await on_event("delta", {"text": piece})
            except Exception as exc:  # noqa: BLE001 - classified below
                msg = str(exc)
                elapsed = (time.perf_counter() - start) * 1000
                if any(marker in msg for marker in _CONTENT_FILTER_MARKERS):
                    return full, None, elapsed, f"content_filter:{msg}"
                if attempt == 1 and first_ms is None:
                    continue  # transient pre-token failure: one silent retry
                return full, None, elapsed, msg
            return full, first_ms, (time.perf_counter() - start) * 1000, None
        return "", None, (time.perf_counter() - start) * 1000, "unreachable"  # pragma: no cover

    @staticmethod
    def _redact_tool_results(messages: list[Any]) -> list[Any]:
        """Replace <tool_result> bodies with metadata stubs so a provider
        content filter that objected to chat contents can still answer
        structural questions (who messaged, when, how many)."""
        out: list[Any] = []
        for m in messages:
            content = getattr(m, "content", None)
            if isinstance(content, str) and "<tool_result>" in content:
                head = content.split("<tool_result>", 1)[0].rstrip()
                out.append(
                    type(m)(
                        content=(
                            f"{head}\n[chat contents removed after provider "
                            "content filter; only counts/names/timestamps available]"
                        ).strip()
                    )
                )
            else:
                out.append(m)
        return out

    async def _correct_json(self, bad_raw: str) -> tuple[dict[str, Any] | None, str]:
        """Single corrective re-ask when a tool-call JSON object is malformed."""
        try:
            fix = await self.llm.ainvoke(
                [
                    HumanMessage(
                        content=(
                            "Your previous reply was not valid JSON per the required schema. "
                            "Respond AGAIN with ONLY the correct JSON object, no prose.\n\n"
                            f"Previous reply:\n{bad_raw[:2000]}"
                        )
                    )
                ]
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"llm correction call failed: {exc}"
        fixed = extract_json(_content_str(fix.content))
        if fixed is None:
            return None, "model did not produce valid JSON"
        return fixed, ""

    def _finish_answer(
        self,
        user_id: str,
        text: str,
        started: float,
        first_token_ms: float | None,
        llm_ms: float,
        rounds: int,
        user_input: str = "",
        effective_input: str = "",
        citations: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        total_ms = (time.perf_counter() - started) * 1000
        self.store.audit(
            "request_completed",
            user_id,
            type="answer",
            rounds=rounds,
            first_token_ms=_ms(first_token_ms),
            llm_ms=_ms(llm_ms),
            total_ms=_ms(total_ms),
            has_citations=bool(citations),
        )
        # Rolling conversation memory (what the supervisor sees next turn).
        self.store.add_memory(user_id, "user", effective_input or user_input)
        self.store.add_memory(user_id, "assistant", text)
        payload: dict[str, Any] = {"text": text, "processTimeMs": _ms(total_ms)}
        if citations:
            payload["citations"] = citations
            payload["grounded"] = True
        elif _requires_whatsapp_tool(effective_input):
            payload["grounded"] = False
        return AgentResult("answer", payload)

    # ------------------------------------------------------------------ #
    # Mutating actions → approvals
    # ------------------------------------------------------------------ #

    async def _create_mutating_action(
        self,
        tool: Tool,
        args: Any,
        user_id: str,
        reason: str,
        started: float,
        first_token_ms: float | None,
        user_input: str = "",
        effective_input: str = "",
    ) -> AgentResult:
        arg_dict = args.model_dump()
        warnings: list[str] = []

        if tool.name == "send_message":
            # Canonical JID or the bridge may hit a slow/invalid-JID path on
            # human-format numbers like "+918252673358" (observed: request
            # hangs until client timeout, message never delivered).
            arg_dict["recipient"] = _normalize_recipient(arg_dict["recipient"])
            # 5 Why: hallucinated JIDs — verify recipient exists in real archive, not imagination
            recipient = arg_dict["recipient"]
            if "@" in recipient and not self.archive.available():
                # If it's a valid phone JID, allow even when archive offline (new contact); else block
                jid_local = recipient.split("@")[0]
                if not (jid_local.isdigit() and 7 <= len(jid_local) <= 15 and "@s.whatsapp.net" in recipient):
                    return AgentResult(
                        "blocked",
                        {"reason": "archive not available — cannot verify recipient in real chats. Ensure WhatsApp is paired and messages.db synced."},
                    )
            if "@" in recipient and self.archive.available():
                try:
                    found = False
                    # Direct JID match in chats (strongest)
                    for c in self.archive.list_chats(limit=200):
                        if c["jid"] == recipient:
                            found = True
                            break
                    if not found:
                        # Search ranked also checks JID digits fallback
                        ranked = self.archive.search_chats_ranked(recipient, limit=5)
                        if any(r.get("jid") == recipient for r in ranked):
                            found = True
                        elif ranked and any(r.get("match") in ("exact", "substring") for r in ranked):
                            # Recipient looks like name fragment but not exact JID — suggest
                            suggestions = ", ".join(f"{r.get('name')} ({r.get('jid')})" for r in ranked[:3])
                            self.store.audit("send_blocked_unknown_recipient", user_id, recipient=recipient)
                            return AgentResult(
                                "blocked",
                                {
                                    "reason": f"recipient not found in real WhatsApp chats. Did you mean: {suggestions}? Call search_chats to resolve."
                                },
                            )
                        elif "@s.whatsapp.net" in recipient or "@g.us" in recipient:
                            # JID format but not in archive — allow if it's a valid phone JID (new contact), block only if clearly hallucinated non-numeric
                            jid_local = recipient.split("@")[0]
                            if jid_local.isdigit() and 7 <= len(jid_local) <= 15:
                                # Valid phone JID, allow even if not in archive (new contact)
                                found = True
                            else:
                                self.store.audit("send_blocked_hallucinated_jid", user_id, recipient=recipient)
                                return AgentResult(
                                    "blocked",
                                    {"reason": "recipient JID not found in real archive — verify with search_chats before sending."},
                                )
                except Exception:
                    pass
            # Also block empty/ malformed JID
            if "@" not in arg_dict["recipient"] and not arg_dict["recipient"].strip():
                return AgentResult("blocked", {"reason": "invalid recipient — must be phone number with country code or JID"})

        if tool.name in ("initiate_audio_call", "initiate_video_call"):
            # Same JID hygiene as send, plus calling note
            arg_dict["recipient"] = _normalize_recipient(arg_dict["recipient"])
            recipient = arg_dict["recipient"]
            if "@" in recipient and not self.archive.available():
                jid_local = recipient.split("@")[0]
                if not (jid_local.isdigit() and 7 <= len(jid_local) <= 15 and "@s.whatsapp.net" in recipient):
                    return AgentResult(
                        "blocked",
                        {"reason": "archive not available — cannot verify call recipient. Ensure WhatsApp is paired."},
                    )
            if "@" in recipient and self.archive.available():
                try:
                    found = False
                    for c in self.archive.list_chats(limit=200):
                        if c["jid"] == recipient:
                            found = True
                            break
                    if not found:
                        ranked = self.archive.search_chats_ranked(recipient, limit=5)
                        if any(r.get("jid") == recipient for r in ranked):
                            found = True
                        elif ranked and any(r.get("match") in ("exact", "substring") for r in ranked):
                            suggestions = ", ".join(f"{r.get('name')} ({r.get('jid')})" for r in ranked[:3])
                            self.store.audit("call_blocked_unknown_recipient", user_id, recipient=recipient)
                            return AgentResult(
                                "blocked",
                                {"reason": f"call recipient not found in real WhatsApp chats. Did you mean: {suggestions}?"},
                            )
                        elif "@s.whatsapp.net" in recipient or "@g.us" in recipient:
                            jid_local = recipient.split("@")[0]
                            if jid_local.isdigit() and 7 <= len(jid_local) <= 15:
                                found = True
                            else:
                                self.store.audit("call_blocked_hallucinated_jid", user_id, recipient=recipient)
                                return AgentResult(
                                    "blocked",
                                    {"reason": "call recipient JID not found — verify with search_chats."},
                                )
                except Exception:
                    pass
            if "@" not in arg_dict["recipient"] and not arg_dict["recipient"].strip():
                return AgentResult("blocked", {"reason": "invalid call recipient — must be phone number with country code or JID"})
            call_type = "video" if tool.name == "initiate_video_call" else "audio"
            warnings.append(f"WhatsApp {call_type} call via linked device may be limited by WhatsApp Web — will be logged; dial manually on phone if needed.")
            # Also add note about permission
            warnings.append("Requires human approval before dialing — say yes to approve.")

        if tool.name == "delete_message":
            target = None
            if self.archive.available():
                target = self.archive.get_message(arg_dict["message_id"], arg_dict["chat_jid"])
            if target is None:
                return AgentResult(
                    "blocked",
                    {
                        "reason": "delete refused: message not found in local archive; "
                        "only account-owned messages can be revoked on WhatsApp"
                    },
                )
            if not target.is_from_me:
                return AgentResult(
                    "blocked",
                    {
                        "reason": "delete refused: this message was not sent by this account. "
                        "WhatsApp does not allow deleting other people's messages."
                    },
                )
            if target.deleted:
                return AgentResult("blocked", {"reason": "message is already deleted"})
            warnings.append(f"original content preview: {_preview(target.content)}")

        self.store.expire_stale()
        action = self.store.create_action(
            user_id=user_id,
            tool=tool.name,
            args=arg_dict,
            reason=reason,
            warnings=warnings,
            ttl_seconds=self.settings.pending_action_ttl_seconds,
        )
        total_ms = (time.perf_counter() - started) * 1000
        self.store.audit(
            "action_created",
            user_id,
            action_id=action.id,
            tool=tool.name,
            warnings=warnings,
            total_ms=_ms(total_ms),
        )
        # Remember who this conversation is working with so "him/her" resolves
        # next turn, plus a memory digest of the proposal.
        contact_jid = arg_dict.get("recipient") or arg_dict.get("chat_jid")
        if contact_jid:
            contact_name = self._contact_name_for_jid(contact_jid)
            self.store.set_last_contact(user_id, contact_jid, contact_name)
        digest = f"[proposed {tool.name} → {contact_jid or 'unknown'}]"
        self.store.add_memory(user_id, "user", effective_input or user_input)
        self.store.add_memory(user_id, "assistant", digest)

        return AgentResult(
            "approval_required",
            {
                "actionId": action.id,
                "tool": action.tool,
                "args": action.args,
                "reason": action.reason,
                "warnings": action.warnings,
                "expiresAt": _iso(action.expires_at),
                "processTimeMs": _ms(total_ms),
            },
        )

    def _contact_name_for_jid(self, jid: str) -> str | None:
        for chat in self.archive.list_chats(limit=100):
            if chat["jid"] == jid:
                return chat.get("name") or None
        return None

    def _update_last_contact_from_tool_results(self, user_id: str, messages: list[Any]) -> None:
        """After an answer, promote the most recently surfaced chat (from the
        newest search/read result) to last-contact so pronouns follow it."""
        import re

        for m in reversed(messages):
            content = getattr(m, "content", None)
            if not isinstance(content, str) or "<tool_result>" not in content:
                continue
            block = content.split("<tool_result>", 1)[1]
            jid_match = re.search(r'"(?:jid|chat_jid)":\s*"([^"]+)"', block)
            if jid_match:
                jid = jid_match.group(1)
                self.store.set_last_contact(user_id, jid, self._contact_name_for_jid(jid))
            return

    async def propose_send(
        self, recipient: str, message: str, user_id: str, quoted_message_id: str | None = None
    ) -> AgentResult:
        """Deterministic send proposal from the Chats UI (no LLM involved).
        Runs the identical approval pipeline."""
        started = time.perf_counter()
        tool = self.registry["send_message"]
        args_model, err = validate_args(
            tool,
            {
                "recipient": recipient,
                "message": message,
                "quoted_message_id": quoted_message_id or None,
            },
        )
        if args_model is None:
            return AgentResult("blocked", {"reason": err})
        return await self._create_mutating_action(
            tool,
            args_model,
            user_id,
            "sent from Chats composer",
            started,
            None,
            user_input=f"composer send to {recipient}",
        )

    # ------------------------------------------------------------------ #
    # Approval decisions
    # ------------------------------------------------------------------ #

    async def decide(
        self, action_id: str, approved: bool, user_id: str
    ) -> tuple[int, dict[str, Any]]:
        action = self.store.get_action(action_id)
        if action is None:
            return 404, {"status": "unknown", "detail": "no such action"}

        decided = self.store.decide_action(action_id, approved)
        if decided is None:
            current = self.store.get_action(action_id)
            status = current.status if current else "unknown"
            code = 410 if status == "expired" else 409
            return code, {"status": status, "detail": "action is no longer pending"}

        if not approved:
            self.store.audit("action_rejected", user_id, action_id=action_id)
            return 200, {"status": "rejected", "actionId": action_id}

        self.store.audit(
            "action_approved", user_id, action_id=action_id, tool=decided.tool, args=decided.args
        )

        exec_started = time.perf_counter()
        try:
            if decided.tool == "send_message":
                resp = await self.bridge.send_message(
                    decided.args["recipient"],
                    decided.args["message"],
                    decided.args.get("quoted_message_id"),
                )
                result: dict[str, Any] = {"bridge": resp.data, "http_status": resp.status}
            elif decided.tool == "delete_message":
                resp = await self.bridge.delete_message(
                    decided.args["chat_jid"], decided.args["message_id"]
                )
                result = {"bridge": resp.data, "http_status": resp.status}
            elif decided.tool == "initiate_audio_call":
                resp = await self.bridge.initiate_audio_call(decided.args["recipient"])
                result: dict[str, Any] = {
                    "bridge": resp.data,
                    "http_status": resp.status,
                    "simulated": bool(resp.data.get("simulated")),
                }
            elif decided.tool == "initiate_video_call":
                resp = await self.bridge.initiate_video_call(decided.args["recipient"])
                result: dict[str, Any] = {
                    "bridge": resp.data,
                    "http_status": resp.status,
                    "simulated": bool(resp.data.get("simulated")),
                }
            else:  # pragma: no cover - registry controls the set
                return 400, {"status": "failed", "detail": f"unsupported tool {decided.tool}"}
        except BridgeError as exc:
            exec_ms = _ms((time.perf_counter() - exec_started) * 1000)
            self.store.complete_action(action_id, success=False, result={"error": str(exc)})
            self.store.audit(
                "action_failed",
                user_id,
                action_id=action_id,
                error=str(exc),
                bridge_exec_ms=exec_ms,
            )
            return 502, {"status": "failed", "detail": str(exc), "actionId": action_id}

        exec_ms = _ms((time.perf_counter() - exec_started) * 1000)
        result["bridgeExecMs"] = exec_ms
        self.store.complete_action(action_id, success=True, result=result)
        self.store.audit(
            "action_executed",
            user_id,
            action_id=action_id,
            tool=decided.tool,
            bridge_exec_ms=exec_ms,
        )
        return 200, {"status": "executed", "actionId": action_id, "result": result}


def _normalize_recipient(recipient: str) -> str:
    """Canonical WhatsApp JID for bare/human-format numbers.

    "918252673358", "+91 82526 73358" → "918252673358@s.whatsapp.net".
    Anything already containing "@" (user JID, group JID, LID) is untouched.
    """
    value = recipient.strip()
    if "@" in value:
        return value
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"{digits}@s.whatsapp.net" if digits else value


def _preview(text: str, width: int = 80) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:width] + ("…" if len(collapsed) > width else "")


def _iso(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _ms(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def new_action_id() -> str:
    return uuid.uuid4().hex
