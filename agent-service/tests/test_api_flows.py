"""End-to-end API flow tests with scripted LLM + fake bridge."""

from __future__ import annotations

import pytest

import app.main as main_mod
from app.main import set_container

from .conftest import auth_headers, build_container


def answer(text="done"):
    return f'{{"type": "answer", "text": "{text}"}}'


SEND_PROPOSAL = (
    '{"type": "tool_call", "tool": "send_message", '
    '"args": {"recipient": "15551110000@s.whatsapp.net", "message": "Reminder: meeting at 3pm"}, '
    '"reason": "user asked to remind Alice"}'
)
DELETE_OWN = (
    '{"type": "tool_call", "tool": "delete_message", '
    '"args": {"chat_jid": "15551110000@s.whatsapp.net", "message_id": "MSG-OWN-1"}, '
    '"reason": "typo fix"}'
)
DELETE_OTHERS = (
    '{"type": "tool_call", "tool": "delete_message", '
    '"args": {"chat_jid": "15551110000@s.whatsapp.net", "message_id": "MSG-THEIR-1"}, '
    '"reason": "user asked"}'
)
GET_MESSAGES_CALL = (
    '{"type": "tool_call", "tool": "get_messages", '
    '"args": {"chat_jid": "15551110000@s.whatsapp.net"}, "reason": "need context"}'
)


@pytest.mark.asyncio
class TestFlows:
    async def test_health(self, authorized_client):
        client, _ = authorized_client
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["provider"] == "agentrouter"
        assert body["archive"]["available"] is True

    async def test_auth_required(self, authorized_client):
        client, _ = authorized_client
        assert (await client.post("/agents/whatsapp", json={"message": "hi"})).status_code == 401
        assert (
            await client.post(
                "/agents/whatsapp",
                json={"message": "hi"},
                headers={"Authorization": "Bearer wrong"},
            )
        ).status_code == 401

    async def test_direct_answer(self, tmp_path):
        container = build_container(tmp_path, [answer("Hello!")])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "ping"}, headers=auth_headers()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "answer"
        assert body["text"] == "Hello!"
        assert isinstance(body["processTimeMs"], (int, float))

    async def test_read_only_tool_round_trip(self, tmp_path):
        container = build_container(tmp_path, [GET_MESSAGES_CALL, answer("Alice said hi")])
        set_container(container)
        assert len(container.llm.calls) == 0
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp",
                json={"message": "what did Alice say?"},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "Alice said hi"
        # two LLM invocations: proposal then final answer
        assert len(container.llm.calls) == 2
        # raw tool result JSON was fed back to the LLM inside a redactable marker
        second_round = "\n".join(str(m.content) for m in container.llm.calls[1])
        assert "TOOL RESULT for get_messages" in second_round
        assert "<tool_result>" in second_round
        assert '"content": "hello from me"' in second_round

    async def test_content_filter_recovers_with_redaction(self, tmp_path):
        """Provider content-block on chat bodies → one redacted retry answers."""
        call = (
            '{"type": "tool_call", "tool": "get_messages", '
            '"args": {"chat_jid": "15551110000@s.whatsapp.net"}, "reason": "ctx"}'
        )
        container = build_container(
            tmp_path,
            [call, Exception("Error code: 400 - 'content-blocked'"), "Who messaged: Alice"],
            llm_max_tool_rounds=3,
        )
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "who messaged?"}, headers=auth_headers()
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "Who messaged: Alice"
        # third LLM round happened with tool-result bodies removed
        assert len(container.llm.calls) == 3
        third_round = "\n".join(str(m.content) for m in container.llm.calls[2])
        assert "hello from me" not in third_round
        assert "chat contents removed after provider" in third_round
        events = [e["event"] for e in container.store.get_audit()]
        assert "llm_content_filter_retry" in events

    async def test_content_filter_twice_returns_clean_error(self, tmp_path):
        container = build_container(
            tmp_path,
            [
                Exception("Error code: 400 - 'content-blocked'"),
                Exception("Error code: 400 - 'content-blocked'"),
            ],
        )
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "do the thing"}, headers=auth_headers()
            )
        assert resp.status_code == 503
        assert "content filter" in resp.json()["message"]

    async def test_user_request_framed_for_provider_filter(self, tmp_path):
        """The provider flags person-lookup-shaped messages ("search deepak
        mandal"); every request must ship inside the framing header."""
        container = build_container(tmp_path, ["plain answer"])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp",
                json={"message": "search deepak mandal"},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        first_human = str(container.llm.calls[0][1].content)
        assert first_human.startswith("[WhatsApp assistant task]\n")
        assert "search deepak mandal" in first_human

    async def test_transient_timeout_retried_before_first_token(self, tmp_path):
        container = build_container(tmp_path, [Exception("Request timed out."), "Recovered answer"])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "please continue"}, headers=auth_headers()
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "Recovered answer"
        assert len(container.llm.calls) == 2

    async def test_send_requires_approval_then_executes(self, tmp_path):
        container = build_container(tmp_path, [SEND_PROPOSAL])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp",
                json={"message": "remind Alice about 3pm"},
                headers=auth_headers(),
            )
            assert resp.status_code == 202
            body = resp.json()
            assert body["type"] == "approval_required"
            action_id = body["actionId"]
            assert body["tool"] == "send_message"
            # nothing sent yet
            assert container.bridge.sent == []

            ok = await ac.post(
                f"/agents/whatsapp/approve/{action_id}",
                json={"approved": True},
                headers=auth_headers(),
            )
            assert ok.status_code == 200
            assert ok.json()["status"] == "executed"
            assert len(container.bridge.sent) == 1
            assert container.bridge.sent[0]["message"] == "Reminder: meeting at 3pm"

    async def test_reject_cancels_action(self, tmp_path):
        container = build_container(tmp_path, [SEND_PROPOSAL])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "send hi"}, headers=auth_headers()
            )
            action_id = resp.json()["actionId"]
            rej = await ac.post(
                f"/agents/whatsapp/approve/{action_id}",
                json={"approved": False},
                headers=auth_headers(),
            )
            assert rej.status_code == 200
            assert rej.json()["status"] == "rejected"
            assert container.bridge.sent == []
            # double decision → conflict
            again = await ac.post(
                f"/agents/whatsapp/approve/{action_id}",
                json={"approved": True},
                headers=auth_headers(),
            )
            assert again.status_code == 409

    async def test_delete_own_message_flow(self, tmp_path):
        container = build_container(tmp_path, [DELETE_OWN])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp",
                json={"message": "delete my last message to Alice"},
                headers=auth_headers(),
            )
            assert resp.status_code == 202
            assert any("preview" in w for w in resp.json()["warnings"])
            action_id = resp.json()["actionId"]

            ok = await ac.post(
                f"/agents/whatsapp/approve/{action_id}",
                json={"approved": True},
                headers=auth_headers(),
            )
            assert ok.status_code == 200
            assert container.bridge.deleted[0]["message_id"] == "MSG-OWN-1"

    async def test_delete_others_message_blocked_before_approval(self, tmp_path):
        container = build_container(tmp_path, [DELETE_OTHERS])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp",
                json={"message": "delete Alice's message"},
                headers=auth_headers(),
            )
            assert resp.status_code == 422
            body = resp.json()
            assert body["type"] == "blocked"
            assert "not sent by this account" in body["reason"]
            assert container.store.list_actions(status="pending") == []

    async def test_plus_prefixed_recipient_normalized(self, tmp_path):
        """Regression: "+918252673358" hung the bridge in an LID-lookup path
        and the message never delivered; it must be stored/execute as a
        canonical JID."""
        plus_proposal = (
            '{"type": "tool_call", "tool": "send_message", '
            '"args": {"recipient": "+918252673358", "message": "hi Mehtab"}, '
            '"reason": "asked"}'
        )
        container = build_container(tmp_path, [plus_proposal])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "message mehtab"}, headers=auth_headers()
            )
            assert resp.status_code == 202
            action_id = resp.json()["actionId"]
            assert resp.json()["args"]["recipient"] == "918252673358@s.whatsapp.net"

            ok = await ac.post(
                f"/agents/whatsapp/approve/{action_id}",
                json={"approved": True},
                headers=auth_headers(),
            )
        assert ok.status_code == 200
        assert ok.json()["status"] == "executed"
        assert container.bridge.sent[0]["recipient"] == "918252673358@s.whatsapp.net"

    async def test_audit_trail_written(self, tmp_path):
        container = build_container(tmp_path, [SEND_PROPOSAL])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            await ac.post(
                "/agents/whatsapp", json={"message": "remind alice"}, headers=auth_headers()
            )
        events = [e["event"] for e in container.store.get_audit()]
        assert "request_received" in events
        assert "action_created" in events


@pytest.mark.asyncio
class TestChatsUI:
    async def test_chats_list_from_archive(self, tmp_path):
        container = build_container(tmp_path, [])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.get("/agents/whatsapp/chats", headers=auth_headers())
            assert resp.status_code == 200
            chats = resp.json()["chats"]
            assert any(c["jid"] == "15551110000@s.whatsapp.net" for c in chats)
            resp = await ac.get("/agents/whatsapp/chats?query=Alice", headers=auth_headers())
            assert all("Alice" in (c["name"] or "") for c in resp.json()["chats"])

    async def test_chat_messages_endpoint(self, tmp_path):
        container = build_container(tmp_path, [])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.get(
                "/agents/whatsapp/chats/messages",
                params={"chat_jid": "15551110000@s.whatsapp.net"},
                headers=auth_headers(),
            )
            assert resp.status_code == 200
            msgs = resp.json()["messages"]
            assert len(msgs) >= 2
            assert msgs[0]["id"] == "MSG-OWN-1"  # chronological order
            assert msgs[-1]["id"] == "MSG-THEIR-1"

    async def test_direct_send_creates_approval_then_executes(self, tmp_path):
        container = build_container(tmp_path, [])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp/send",
                json={
                    "recipient": "15551110000@s.whatsapp.net",
                    "message": "direct composer test",
                },
                headers=auth_headers(),
            )
            assert resp.status_code == 202
            body = resp.json()
            assert body["type"] == "approval_required"
            assert container.bridge.sent == []
            ok = await ac.post(
                f"/agents/whatsapp/approve/{body['actionId']}",
                json={"approved": True},
                headers=auth_headers(),
            )
            assert ok.status_code == 200
            assert ok.json()["status"] == "executed"
            assert len(container.bridge.sent) == 1


@pytest.mark.asyncio
class TestGreetingFastPath:
    async def test_greeting_answers_instantly_without_llm(self, tmp_path):
        container = build_container(tmp_path, [])  # no scripted replies needed
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            resp = await ac.post(
                "/agents/whatsapp", json={"message": "hii"}, headers=auth_headers()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "WhatsApp copilot" in body["text"]
        assert container.llm.calls == []  # zero LLM hops
        assert body["processTimeMs"] < 200

    async def test_greeting_variants(self, tmp_path):
        container = build_container(tmp_path, [])
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            for msg in ["hello!", "Hey", "GOOD MORNING", "namaste"]:
                resp = await ac.post(
                    "/agents/whatsapp", json={"message": msg}, headers=auth_headers()
                )
                assert resp.status_code == 200, msg
                assert "copilot" in resp.json()["text"], msg

    async def test_stage_events_in_stream(self, tmp_path):
        from .test_stream import collect_sse

        container = build_container(tmp_path, ["plain done"], llm_max_tool_rounds=2)
        set_container(container)
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app), base_url="http://t"
        ) as ac:
            events = await collect_sse(ac, "tell me something")
        names = [e for e, _ in events]
        assert "stage" in names
        phases = [d.get("phase") for e, d in events if e == "stage"]
        assert phases[0] == "thinking"
        assert names[-1] == "answer"
