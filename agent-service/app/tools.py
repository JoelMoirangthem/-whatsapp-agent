"""Tool registry for the WhatsApp agent.

Read-only tools execute immediately (results feed back to the LLM).
Mutating tools NEVER execute directly — the orchestrator converts them into
pending approval actions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.bridge import MessageArchive

# ---------------------------------------------------------------------------
# Argument schemas (validated before anything executes)
# ---------------------------------------------------------------------------


class ListChatsArgs(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class GetMessagesArgs(BaseModel):
    chat_jid: str = Field(min_length=3)
    limit: int = Field(default=50, ge=1, le=200)
    after_date: str | None = None
    before_date: str | None = None


class SearchChatsArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class SendMessageArgs(BaseModel):
    recipient: str = Field(min_length=6)
    message: str = Field(min_length=1)
    quoted_message_id: str | None = None


class DeleteMessageArgs(BaseModel):
    chat_jid: str = Field(min_length=3)
    message_id: str = Field(min_length=1)


class InitiateAudioCallArgs(BaseModel):
    recipient: str = Field(min_length=6, description="phone number with country code or JID to audio call")


class InitiateVideoCallArgs(BaseModel):
    recipient: str = Field(min_length=6, description="phone number with country code or JID to video call")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    mutates: bool
    args_model: type[BaseModel]
    executor: Callable[..., Awaitable[dict[str, Any]]] | None = None
    precheck: Callable[[BaseModel], Awaitable[str | None]] | None = None
    prompt_hint: str = ""


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width] + "…"


def make_registry(
    archive: MessageArchive,
    max_content_chars_per_message: int = 500,
    max_context_chars: int = 8000,
) -> dict[str, Tool]:
    """Build executors bound to the local message archive."""

    async def list_chats(args: ListChatsArgs) -> dict[str, Any]:
        return {"chats": archive.list_chats(limit=args.limit)}

    async def get_messages(args: GetMessagesArgs) -> dict[str, Any]:
        messages = archive.get_messages(
            args.chat_jid,
            limit=args.limit,
            after_date=args.after_date,
            before_date=args.before_date,
        )
        total = 0
        out: list[dict[str, Any]] = []
        for msg in reversed(messages):  # chronological order for the LLM
            entry = {
                "id": msg.id,
                "sender": msg.sender or ("me" if msg.is_from_me else "unknown"),
                "from_me": msg.is_from_me,
                "time": msg.timestamp,
                "content": _truncate(msg.content, max_content_chars_per_message),
                "type": msg.media_type or "text",
            }
            if msg.media_type and msg.media_type not in ("text",):
                entry["media"] = f"[{msg.media_type} received]" if not msg.content.strip() else ""
                entry["media"] = entry["media"].strip()
                if not entry["media"]:
                    del entry["media"]
            if msg.deleted:
                entry["deleted_by_sender"] = True
            total += len(str(entry["content"]))
            out.append(entry)
            if total > max_context_chars:
                break
        return {"chat_jid": args.chat_jid, "messages": out}

    async def search_chats(args: SearchChatsArgs) -> dict[str, Any]:
        ranked = archive.search_chats_ranked(args.query, limit=8)
        if not ranked:
            return {
                "matches": [],
                "note": "no chat matched; try fewer letters of the name or a phone number",
            }
        exact = [c for c in ranked if c["match"] in ("exact", "substring")]
        return {
            "matches": ranked,
            "best_match": ranked[0],
            "note": (
                "exact match found"
                if exact
                else "no exact match — these are similar names; confirm with the "
                "user before sending anything"
            ),
        }

    tools = [
        Tool(  # type: ignore[misc]
            name="list_chats",
            description="List recent WhatsApp chats with names and JIDs.",
            mutates=False,
            args_model=ListChatsArgs,
            executor=list_chats,
        ),
        Tool(  # type: ignore[misc]
            name="get_messages",
            description=(
                "Read recent messages from one chat for analysis/summarization. "
                "This RETURNS THE ACTUAL MESSAGE CONTENTS from the local archive "
                "immediately — use it whenever the user wants to read, see, show, "
                "or analyze any conversation."
            ),
            mutates=False,
            args_model=GetMessagesArgs,
            executor=get_messages,
        ),
        Tool(  # type: ignore[misc]
            name="search_chats",
            description=(
                "Find chats by contact name or phone number. Exact matches rank "
                "first; when none exists, similar names are returned with a "
                "'similar' flag for user confirmation."
            ),
            mutates=False,
            args_model=SearchChatsArgs,
            executor=search_chats,
        ),
        Tool(  # type: ignore[misc]
            name="send_message",
            description=(
                "Send a WhatsApp text message to a phone number or group JID. "
                "Emitting this tool_call creates a pending action that the "
                "system routes for human approval automatically."
            ),
            mutates=True,
            args_model=SendMessageArgs,
            prompt_hint="recipient must be a phone number with country code or a full JID",
        ),
        Tool(  # type: ignore[misc]
            name="delete_message",
            description=(
                "Delete-for-everyone a message previously sent by this account. "
                "Emitting this tool_call creates a pending action that the "
                "system routes for human approval automatically."
            ),
            mutates=True,
            args_model=DeleteMessageArgs,
            prompt_hint="only messages sent by this account can be deleted",
        ),
        Tool(  # type: ignore[misc]
            name="initiate_audio_call",
            description=(
                "YOU CAN PLACE WHATSAPP AUDIO CALLS — use this tool when user says 'call', 'audio call', 'phone' etc. "
                "Emitting this tool_call creates a pending approval; the system will then attempt the call (linked-device may be simulated but you MUST try). "
                "NEVER say you cannot place calls or offer to draft a message instead — you MUST emit this tool_call with recipient JID."
            ),
            mutates=True,
            args_model=InitiateAudioCallArgs,
            prompt_hint="recipient must be phone number with country code or full JID; approval required; YOU CAN PLACE CALLS",
        ),
        Tool(  # type: ignore[misc]
            name="initiate_video_call",
            description=(
                "YOU CAN PLACE WHATSAPP VIDEO CALLS — use this tool when user says 'video call' etc. "
                "Emitting this tool_call creates a pending approval; the system will then attempt the call (linked-device may be simulated but you MUST try). "
                "NEVER say you cannot place calls — you MUST emit this tool_call with recipient JID."
            ),
            mutates=True,
            args_model=InitiateVideoCallArgs,
            prompt_hint="recipient must be phone number with country code or full JID; approval required; YOU CAN PLACE CALLS",
        ),
    ]
    return {tool.name: tool for tool in tools}


def validate_args(tool: Tool, args: Any) -> tuple[BaseModel | None, str]:
    """Parse+validate arguments against the tool's schema."""
    if not isinstance(args, dict):
        return None, "args must be a JSON object"
    try:
        return tool.args_model.model_validate(args), ""
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        return None, f"invalid arguments: {errors}"
