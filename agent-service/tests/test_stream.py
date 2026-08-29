"""Streaming endpoint tests: SSE sequencing, modes, latency headers."""

from __future__ import annotations

import json
from typing import Any

import pytest

import app.main as main_mod
from app.main import set_container

from .conftest import auth_headers, build_container


async def collect_sse(client, message: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    async with client.stream(
        "POST",
        "/agents/whatsapp/stream",
        json={"message": message},
        headers=auth_headers(),
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                event_name, data_line = None, ""
                for line in block.split("\n"):
                    if line.startswith("event:"):
                        event_name = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_line += line[len("data:") :].strip()
                if event_name and data_line:
                    events.append((event_name, json.loads(data_line)))
    return events


def by_name(events, name):
    return [data for evt, data in events if evt == name]


@pytest.mark.asyncio
class TestStream:
    async def test_plain_text_answer_streams_deltas(self, tmp_path):
        container = build_container(tmp_path, ["Hello there friend"])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            events = await collect_sse(ac, "summarize my recent chats")

        names = [e for e, _ in events]
        assert names[0] == "meta"
        assert names[-1] == "answer"
        deltas = by_name(events, "delta")
        assert deltas, "plain-text answers must stream delta events"
        joined = "".join(d["text"] for d in deltas)
        assert joined.strip() == "Hello there friend"
        final = events[-1][1]
        assert final["type"] == "answer"
        assert isinstance(final["processTimeMs"], (int, float))

    async def test_json_answer_shape_still_supported(self, tmp_path):
        container = build_container(tmp_path, ['{"type": "answer", "text": "ok"}'])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            events = await collect_sse(ac, "give me a status update")

        assert by_name(events, "delta") == []  # JSON mode buffers, never streams
        final = events[-1]
        assert final[0] == "answer"
        assert final[1]["text"] == "ok"

    async def test_stream_proposal_then_approve_executes(self, tmp_path):
        proposal = (
            '{"type": "tool_call", "tool": "send_message", '
            '"args": {"recipient": "15551110000@s.whatsapp.net", "message": "ping"}, '
            '"reason": "asked"}'
        )
        container = build_container(tmp_path, [proposal])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            events = await collect_sse(ac, "send ping")
            final = events[-1]
            assert final[0] == "approval_required"
            action_id = final[1]["actionId"]
            assert container.bridge.sent == []

            ok = await ac.post(
                f"/agents/whatsapp/approve/{action_id}",
                json={"approved": True},
                headers=auth_headers(),
            )
        assert ok.status_code == 200
        assert ok.json()["status"] == "executed"
        assert len(container.bridge.sent) == 1

    async def test_tool_event_emitted_before_answer(self, tmp_path):
        call = (
            '{"type": "tool_call", "tool": "get_messages", '
            '"args": {"chat_jid": "15551110000@s.whatsapp.net"}, "reason": "ctx"}'
        )
        container = build_container(tmp_path, [call, "Alice said hi back"])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            events = await collect_sse(ac, "what did alice say?")

        tools = by_name(events, "tool")
        assert tools and tools[0]["tool"] == "get_messages"
        assert events[-1][0] == "answer"
        assert events[-1][1]["text"] == "Alice said hi back"

    async def test_latency_header_on_all_responses(self, authorized_client):
        client, _ = authorized_client
        health = await client.get("/health")
        assert "X-Process-Time-Ms" in health.headers
        body = health.json()
        assert body["latency"]["budget_warn_ms"] == 2000
        agent_resp = await client.post(
            "/agents/whatsapp", json={"message": "hi"}, headers=auth_headers()
        )
        assert "X-Process-Time-Ms" in agent_resp.headers
