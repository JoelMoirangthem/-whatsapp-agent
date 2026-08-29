"""Shared test fixtures: temp stores, fake archive/bridge/LLM."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response

import app.main as main_mod
from app.bridge import BridgeResponse
from app.config import load_settings_for_test
from app.db import AgentStore

# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #


class FakeLLM:
    """Scripted LLM: pops canned responses per call.

    Supports invoke(), ainvoke() and astream(); plain-text replies are
    streamed word-by-word through astream to exercise real token delivery,
    while JSON replies arrive as a single chunk.
    """

    def __init__(self, replies: list[Any]):
        self.replies = list(replies)
        self.calls: list[list[Any]] = []

    def _next(self, messages) -> Any:
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @staticmethod
    def _as_message(reply: Any) -> Any:
        from langchain_core.messages import AIMessage

        if not isinstance(reply, str):
            return reply  # assume a LangChain-style message object
        return AIMessage(content=reply)

    def invoke(self, messages):
        return self._as_message(self._next(messages))

    async def ainvoke(self, messages):
        return self.invoke(messages)

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        reply = self._next(messages)
        if not isinstance(reply, str):
            yield AIMessageChunk(content=str(getattr(reply, "content", reply)))
            return
        if reply.lstrip().startswith("{"):
            yield AIMessageChunk(content=reply)
            return
        words = reply.split(" ")
        for i, word in enumerate(words):
            tail = " " if i < len(words) - 1 else ""
            yield AIMessageChunk(content=word + tail)


@dataclass
class FakeArchive:
    available_flag: bool = True
    chats: list[dict] = field(default_factory=list)
    messages: dict[tuple[str, str], dict] = field(default_factory=dict)

    def available(self) -> bool:
        return self.available_flag

    def list_chats(self, limit: int = 20) -> list[dict]:
        return self.chats[:limit]

    def get_messages(self, chat_jid, limit=50, after_date=None, before_date=None):
        rows = [m for (mid, cj), m in self.messages.items() if cj == chat_jid]
        return sorted(rows, key=lambda m: m["timestamp"], reverse=True)[:limit]

    def get_message(self, message_id, chat_jid):
        return self.messages.get((message_id, chat_jid))

    def own_sender(self):
        return None

    def search_chats_ranked(self, query, limit=8):
        needle = query.lower()
        out = []
        for c in self.list_chats(limit=100):
            hay = (c["name"] or "").lower()
            if needle in hay or needle in c["jid"].lower():
                out.append({**c, "match": "substring", "score": 0.9})
        return out[:limit]


class FakeBridge:
    def __init__(self):
        self.sent: list[dict] = []
        self.deleted: list[dict] = []
        self.calls: list[dict] = []
        self.fail_on_send = False

    async def health(self):
        return BridgeResponse(True, 200, {"status": "ok"})

    async def send_message(self, recipient, message, quoted=None):
        if self.fail_on_send:
            from app.bridge import BridgeError

            raise BridgeError("bridge down")
        self.sent.append({"recipient": recipient, "message": message})
        return BridgeResponse(True, 200, {"success": True, "message": "sent"})

    async def delete_message(self, chat_jid, message_id):
        self.deleted.append({"chat_jid": chat_jid, "message_id": message_id})
        return BridgeResponse(
            True, 200, {"success": True, "message": f"Message {message_id} deleted for everyone"}
        )

    avatar_bytes = None
    reacted: list[dict] = []

    async def react(self, chat_jid, message_id, emoji, from_me, sender_jid=None):
        self.reacted.append({
            "chat_jid": chat_jid, "message_id": message_id,
            "emoji": emoji, "from_me": from_me, "sender_jid": sender_jid,
        })
        from app.bridge import BridgeResponse
        return BridgeResponse(True, 200, {"ok": True})

    async def get_avatar(self, jid, refresh=False):
        if self.avatar_bytes:
            return True, self.avatar_bytes, "image/jpeg", ""
        return False, None, "", "no profile picture available"

    async def initiate_audio_call(self, recipient: str):
        self.calls.append({"recipient": recipient, "type": "audio"})
        return BridgeResponse(True, 200, {"success": True, "message": "Audio call not supported via WhatsApp Web — logged for manual dial.", "simulated": True, "call_type": "audio"})

    async def initiate_video_call(self, recipient: str):
        self.calls.append({"recipient": recipient, "type": "video"})
        return BridgeResponse(True, 200, {"success": True, "message": "Video call not supported via WhatsApp Web — logged for manual dial.", "simulated": True, "call_type": "video"})

    async def initiate_call(self, recipient: str, is_video: bool = False):
        if is_video:
            return await self.initiate_video_call(recipient)
        return await self.initiate_audio_call(recipient)

    async def aclose(self):
        pass


def make_archive_db(tmp_path) -> str:
    """Create a minimal messages.db shaped like the bridge's."""
    path = tmp_path / "messages.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
        CREATE TABLE messages (
            id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
            is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT,
            media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB,
            file_length INTEGER, deleted_at TIMESTAMP,
            PRIMARY KEY (id, chat_jid));
        """
    )
    now = time.time()
    conn.execute(
        "INSERT INTO chats VALUES (?, ?, ?)",
        ("15551110000@s.whatsapp.net", "Alice", now),
    )
    conn.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 0, NULL)",
        (
            "MSG-OWN-1",
            "15551110000@s.whatsapp.net",
            "15559990000",
            "hello from me",
            now - 60,
            1,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 0, NULL)",
        ("MSG-THEIR-1", "15551110000@s.whatsapp.net", "15551110000", "hi back", now - 30, 0, None),
    )
    conn.commit()
    conn.close()
    return str(path)


def build_container(
    tmp_path, llm_replies: list[str], *, archive=None, bridge=None, **setting_overrides
):
    from app.bridge import MessageArchive
    from app.main import Container

    settings_kwargs = dict(
        agent_db_path=str(tmp_path / "agent.db"),
        messages_db_path=str(tmp_path / "messages.db"),
    )
    settings_kwargs.update(setting_overrides)
    settings = load_settings_for_test(**settings_kwargs)
    container = Container.__new__(Container)
    container.settings = settings
    container.store = AgentStore(settings.agent_db_path)
    container.archive = (
        archive if archive is not None else MessageArchive(make_archive_db(tmp_path))
    )
    container.bridge = bridge if bridge is not None else FakeBridge()
    container.llm = FakeLLM(llm_replies)
    from app.voice import ElevenLabsClient

    container.voice = ElevenLabsClient(
        settings.elevenlabs_api_key,
        settings.elevenlabs_voice_id,
        settings.elevenlabs_model,
    )
    container.agent = __import__("app.agent", fromlist=["WhatsAppAgent"]).WhatsAppAgent(
        settings, container.store, container.archive, container.bridge, container.llm
    )
    return container


@pytest_asyncio.fixture
async def client():
    """Unauthenticated AsyncClient against the real FastAPI app."""
    transport = ASGITransport(app=main_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def auth_headers(token: str = "svc-token") -> dict:
    return {"Authorization": f"Bearer {token}", "X-User-ID": "tester"}


@pytest.fixture
def no_container():
    main_mod.set_container(None)
    yield
    main_mod.set_container(None)


@pytest_asyncio.fixture
async def authorized_client(tmp_path, no_container):
    container = build_container(
        tmp_path,
        llm_replies=['{"type": "answer", "text": "ok"}'],
    )
    main_mod.set_container(container)
    transport = ASGITransport(app=main_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, container


# keep ruff happy about unused import used only for typing
_ = Response
