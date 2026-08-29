"""Supervisor node + conversation memory + fuzzy contact search tests."""

from __future__ import annotations

import sqlite3

import pytest

import app.main as main_mod
from app.bridge import MessageArchive
from app.main import set_container

from .conftest import auth_headers, build_container


@pytest.mark.asyncio
class TestSupervisor:
    async def test_off_topic_short_circuits_without_agent(self, tmp_path):
        # 5 Why: previously blocked off_topic → not normal conversation. Now bypasses and answers normally.
        container = build_container(tmp_path, ['{"intent": "off_topic"}', 'Paris is the capital of France.'], supervisor_enabled=True)
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp",
                json={"message": "what is the capital of France?"},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        # Now allows normal conversation, not the old WhatsApp-only refusal
        assert "only help with this WhatsApp account" not in resp.json()["text"]
        assert "Paris" in resp.json()["text"]
        # Supervisor + agent loop both ran (no short-circuit)
        assert len(container.llm.calls) == 2
        events = [e["event"] for e in container.store.get_audit()]
        assert "supervisor_off_topic_bypass" in events

    async def test_supervisor_rewrite_reaches_agent(self, tmp_path):
        rewritten = (
            '{"intent": "whatsapp_task", "request": '
            '"What did Alice (15551110000@s.whatsapp.net) say in her last message?"}'
        )
        call = (
            '{"type": "tool_call", "tool": "get_messages", '
            '"args": {"chat_jid": "15551110000@s.whatsapp.net"}, "reason": "ctx"}'
        )
        container = build_container(
            tmp_path, [rewritten, call, "Alice said hi"], supervisor_enabled=True
        )
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "what did she say?"}, headers=auth_headers()
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "Alice said hi"
        # The agent loop received the REWRITTEN request, not the pronoun.
        agent_round = "\n".join(str(m.content) for m in container.llm.calls[1])
        assert "What did Alice (15551110000@s.whatsapp.net)" in agent_round
        assert "what did she say?" not in agent_round

    async def test_supervisor_failure_degrades_to_passthrough(self, tmp_path):
        container = build_container(
            tmp_path,
            [Exception("supervisor exploded"), "Direct answer"],
            supervisor_enabled=True,
        )
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "hello there"}, headers=auth_headers()
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "Direct answer"

    async def test_conversation_memory_written_and_loaded(self, tmp_path):
        container = build_container(tmp_path, ["First answer"])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            await ac.post("/agents/whatsapp", json={"message": "turn one"}, headers=auth_headers())
        memory = container.store.get_memory("tester")
        assert [m["role"] for m in memory] == ["user", "assistant"]
        assert memory[0]["content"] == "turn one"
        assert memory[1]["content"] == "First answer"


class TestFuzzySearch:
    def _db_with_names(self, tmp_path):
        path = tmp_path / "fuzzy.db"
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
        rows = [
            ("918077855908@s.whatsapp.net", "Deepak Mandal"),
            ("919812345678@s.whatsapp.net", "Deepak Kumar"),
            ("919898765432@s.whatsapp.net", "Dipak Mandal"),
            ("917409193202@s.whatsapp.net", "Chiranjeet(Sitare)"),
        ]
        for jid, name in rows:
            conn.execute("INSERT INTO chats VALUES (?, ?, 0)", (jid, name))
        conn.commit()
        conn.close()
        return MessageArchive(str(path))

    def test_exact_match_ranks_first(self, tmp_path):
        archive = self._db_with_names(tmp_path)
        res = archive.search_chats_ranked("deepak mandal")
        assert res[0]["name"] == "Deepak Mandal"
        assert res[0]["match"] == "exact"

    def test_similar_names_suggested_when_no_exact(self, tmp_path):
        archive = self._db_with_names(tmp_path)
        res = archive.search_chats_ranked("depek mandl")  # typos, no exact hit
        names = [r["name"] for r in res]
        assert "Deepak Mandal" in names or "Dipak Mandal" in names
        assert all(r["match"] in ("similar", "substring", "exact") for r in res)

    def test_bare_local_number_resolves_via_jid_digits(self, tmp_path):
        archive = self._db_with_names(tmp_path)
        res = archive.search_chats_ranked("8077855908")
        assert res and res[0]["jid"] == "918077855908@s.whatsapp.net"
        assert res[0]["match"] == "exact"

    def test_unrelated_query_returns_empty(self, tmp_path):
        archive = self._db_with_names(tmp_path)
        assert archive.search_chats_ranked("zzzqqq wwwxxxyy") == []


@pytest.mark.asyncio
class TestFastPathSend:
    async def test_direct_send_skips_agent_loop(self, tmp_path):
        """Supervisor resolves contact+text from memory → ONE llm hop total."""
        verdict = (
            '{"intent": "whatsapp_task", '
            '"request": "send Deepak Mandal (918077855908@s.whatsapp.net) a focus reminder", '
            '"direct": {"tool": "send_message", '
            '"recipient_jid": "918077855908@s.whatsapp.net", '
            '"message": "please focus on what you were doing"}}'
        )
        container = build_container(tmp_path, [verdict], supervisor_enabled=True)
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp",
                json={"message": "send him please focus on what you were doing"},
                headers=auth_headers(),
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["tool"] == "send_message"
        assert body["args"]["recipient"] == "918077855908@s.whatsapp.net"
        # exactly ONE llm call (the supervisor) — no agent rounds
        assert len(container.llm.calls) == 1
        assert container.bridge.sent == []  # still gated by approval
        events = [e["event"] for e in container.store.get_audit()]
        assert "fastpath_send" in events
        # last-contact promoted so future pronouns resolve here
        jid, name = container.store.get_last_contact("tester")
        assert jid == "918077855908@s.whatsapp.net"

    async def test_direct_without_jid_falls_back_to_loop(self, tmp_path):
        container = build_container(
            tmp_path,
            [
                '{"intent": "whatsapp_task", "request": "send hi to Alice"}',
                '{"type": "tool_call", "tool": "search_chats", '
                '"args": {"query": "Alice"}, "reason": "resolve"}',
                "Asked Alice?",
            ],
            supervisor_enabled=True,
        )
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "send hi"}, headers=auth_headers()
            )
        assert resp.status_code == 200  # normal loop answered
        assert len(container.llm.calls) == 3
        events = [e["event"] for e in container.store.get_audit()]
        assert "fastpath_send" not in events


@pytest.mark.asyncio
async def test_pronoun_context_injected_from_last_contact(tmp_path):
    """Unresolved pronoun + known last-contact → mechanical context hint."""
    container = build_container(tmp_path, ["Analysed the chat."])
    container.store.set_last_contact("tester", "917409193202@s.whatsapp.net", "Chiranjeet(Sitare)")
    set_container(container)
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=main_mod.app), base_url="http://t") as ac:
        resp = await ac.post(
            "/agents/whatsapp",
            json={"message": "ok analyse it and tell me what he wants"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    first_human = str(container.llm.calls[0][1].content)
    assert "[context] The pronoun" in first_human
    assert "Chiranjeet(Sitare) (917409193202@s.whatsapp.net)" in first_human


@pytest.mark.asyncio
async def test_voice_mode_enforces_short_replies(tmp_path):
    """voice_mode=True injects the brevity contract; default leaves it out."""
    container = build_container(tmp_path / "v1", ["Short spoken answer"])
    set_container(container)
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=main_mod.app), base_url="http://t"
    ) as ac:
        await ac.post(
            "/agents/voice/chat",
            json={"message": "what did chiranjeet say"},
            headers=auth_headers(),
        )
    system_msgs = [str(m.content) for m in container.llm.calls[0] ]
    assert any("VOICE MODE" in c for c in system_msgs)

    container2 = build_container(tmp_path / "v2", ["Normal answer"])
    set_container(container2)
    async with AsyncClient(
        transport=ASGITransport(app=main_mod.app), base_url="http://t"
    ) as ac:
        await ac.post(
            "/agents/whatsapp", json={"message": "hi there"}, headers=auth_headers()
        )
    text_mode = "\n".join(str(m.content) for m in container2.llm.calls[0])
    assert "VOICE MODE" not in text_mode
