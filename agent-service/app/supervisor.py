"""Supervisor node: intent classification + context-aware rewriting.

One fast LLM pass before the agent loop:
- classifies the message as a WhatsApp task or off-topic (off-topic never
  reaches the tool loop),
- rewrites vague references ("send HIM the agenda") into a self-contained
  instruction using rolling conversation memory and the last resolved contact,
- degrades gracefully: on any failure the original input passes through so
  the assistant never becomes unavailable because of supervisor trouble.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db import AgentStore
from app.llm import extract_json

OFF_TOPIC_REPLY = (
    "I can only help with this WhatsApp account — searching chats, reading "
    "and summarizing conversations, or drafting messages to send."
)


@dataclass(frozen=True)
class SupervisorResult:
    request: str  # possibly rewritten, self-contained instruction
    intent: str  # "whatsapp_task" | "off_topic"
    reply: str | None  # direct answer when off-topic
    rewrote: bool  # True when the supervisor changed the request
    direct: dict | None = None  # {"tool","recipient_jid","message"} for one-hop sends


class Supervisor:
    def __init__(self, llm, store: AgentStore):
        # Cap output tokens: the verdict JSON is tiny; this keeps the extra
        # hop cheap even when the provider is slow to stop generating.
        self.llm = llm.bind(max_tokens=250) if hasattr(llm, "bind") else llm
        self.store = store

    def _prompt(self, user_id: str, user_input: str) -> list:
        from langchain_core.messages import HumanMessage, SystemMessage

        memory = self.store.get_memory(user_id, limit=10)
        history = (
            "\n".join(f"- {m['role']}: {m['content'][:280]}" for m in memory)
            or "(this is the first message of the conversation)"
        )
        jid, name = self.store.get_last_contact(user_id)
        last_contact = f"{name} ({jid})" if jid else "none yet"

        system = """You are the supervisor of a WhatsApp assistant. Classify the user's latest message and, when needed, rewrite it into a fully self-contained instruction for the worker agent.

RULES:
1. intent is "off_topic" ONLY when the message is unrelated to operating this WhatsApp account (general knowledge, coding, news, other apps). Everything about chats, contacts, messages, sending, deleting, summarizing, audio/video calls/calling is "whatsapp_task".
2. For whatsapp_task, "request" MUST be self-contained: replace he/him/she/her/they/them/that contact/the same person/the conversation/chat with the concrete name (and phone/JID when known). PRONOUN PRECEDENCE: a pronoun refers to "LAST CONTACT RESOLVED" below unless the immediately preceding exchange explicitly names a DIFFERENT person. Never substitute a person who merely appeared earlier in history when a more recent chat/read exists. Keep established names, numbers and wording otherwise untouched.
3. If the message already stands alone, copy it verbatim into "request".
4. ONE-HOP SEND FAST PATH: when the task is a simple SEND whose exact recipient JID is already known from history AND the exact message text is present in the user's words (no clarification needed), also include "direct" so the system can create the approval in a single step:
   {"intent": "whatsapp_task", "request": "<self-contained instruction>",
    "direct": {"tool": "send_message", "recipient_jid": "<full jid>", "message": "<exact text>"}}
   Omit "direct" whenever the recipient is unknown, ambiguous, or the message wording is unclear.
5. Respond with EXACTLY ONE JSON object and nothing else:
   {"intent": "whatsapp_task", "request": "<self-contained instruction>"}
   {"intent": "off_topic"}"""
        human = (
            f"CONVERSATION SO FAR (oldest first):\n{history}\n\n"
            f"LAST CONTACT RESOLVED: {last_contact}\n\n"
            f'LATEST USER MESSAGE:\n"""{user_input}"""'
        )
        return [SystemMessage(content=system), HumanMessage(content=human)]

    async def review(self, user_id: str, user_input: str) -> SupervisorResult:
        try:
            resp = await self.llm.ainvoke(self._prompt(user_id, user_input))
            content = resp.content if isinstance(resp.content, str) else str(resp.content)
            parsed = extract_json(content)
            if parsed is None:
                return SupervisorResult(user_input, "whatsapp_task", None, False)
            if parsed.get("intent") == "off_topic":
                return SupervisorResult(user_input, "off_topic", OFF_TOPIC_REPLY, False)
            request = str(parsed.get("request") or "").strip()
            direct = parsed.get("direct")
            direct = (
                {
                    "tool": str(direct.get("tool", "")),
                    "recipient_jid": str(direct.get("recipient_jid", "")).strip(),
                    "message": str(direct.get("message", "")).strip(),
                }
                if isinstance(direct, dict)
                and direct.get("tool") == "send_message"
                and str(direct.get("recipient_jid", "")).strip()
                and str(direct.get("message", "")).strip()
                else None
            )
            return SupervisorResult(
                request or user_input,
                "whatsapp_task",
                None,
                request != user_input,
                direct=direct,
            )
        except Exception:  # noqa: BLE001 - supervisor must never block the agent
            return SupervisorResult(user_input, "whatsapp_task", None, False)
