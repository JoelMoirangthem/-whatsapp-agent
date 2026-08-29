"""Media serving + contact-profile enrichment tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _make_stores(tmp_path: Path):
    """messages.db + whatsapp.db + one real image file on disk."""
    store = tmp_path / "store"
    chat_dir = store / "917409193202@s.whatsapp.net"
    chat_dir.mkdir(parents=True)
    img = chat_dir / "image_20260826_010101_ABC123.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fakejpegbytes")

    conn = sqlite3.connect(store / "messages.db")
    conn.executescript(
        """
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
        CREATE TABLE messages (
            id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
            is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT,
            media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB,
            file_length INTEGER, deleted_at TIMESTAMP, quoted_message_id TEXT,
            PRIMARY KEY (id, chat_jid));
        INSERT INTO chats VALUES ('917409193202@s.whatsapp.net', '917409193202', 1);
        INSERT INTO messages VALUES ('IMG-1', '917409193202@s.whatsapp.net', '917409193202',
            'nice photo', 1, 0, 'image', 'image_20260826_010101_ABC123.jpg',
            NULL, NULL, NULL, NULL, NULL, NULL, NULL);
        INSERT INTO messages VALUES ('DOC-1', '917409193202@s.whatsapp.net', '917409193202',
            '', 2, 0, 'document', 'missing_file.pdf',
            NULL, NULL, NULL, NULL, NULL, NULL, NULL);
        """
    )
    conn.commit()
    conn.close()

    wc = sqlite3.connect(store / "whatsapp.db")
    wc.execute(
        "CREATE TABLE whatsmeow_contacts (our_jid TEXT, their_jid TEXT, first_name TEXT,"
        " full_name TEXT, push_name TEXT, business_name TEXT, redacted_phone TEXT)"
    )
    wc.execute(
        "INSERT INTO whatsmeow_contacts VALUES ('me:1@s.whatsapp.net',"
        " '917409193202@s.whatsapp.net', 'Chiranjeet', 'Chiranjeet (Sitare)',"
        " 'chiru', NULL, NULL)"
    )
    wc.commit()
    wc.close()
    return store


class TestProfileEnrichment:
    def test_numeric_chat_name_upgraded_to_contact_name(self, tmp_path):
        from app.bridge import MessageArchive

        store = _make_stores(tmp_path)
        archive = MessageArchive(str(store / "messages.db"))
        chats = archive.list_chats(limit=10)
        assert chats[0]["name"] == "Chiranjeet (Sitare)"

    def test_resolve_contact_by_bare_number(self, tmp_path):
        from app.bridge import MessageArchive

        store = _make_stores(tmp_path)
        archive = MessageArchive(str(store / "messages.db"))
        assert archive.resolve_contact_name("917409193202") == "Chiranjeet (Sitare)"
        assert archive.resolve_contact_name("9999999999") is None


class TestMediaPath:
    def test_existing_blob_resolves_inside_chat_dir(self, tmp_path):
        from app.bridge import MessageArchive

        store = _make_stores(tmp_path)
        archive = MessageArchive(str(store / "messages.db"))
        path = archive.media_path("917409193202@s.whatsapp.net", "IMG-1", str(store))
        assert path is not None and Path(path).read_bytes().startswith(b"\xff\xd8")

    def test_missing_blob_returns_none(self, tmp_path):
        from app.bridge import MessageArchive

        store = _make_stores(tmp_path)
        archive = MessageArchive(str(store / "messages.db"))
        assert archive.media_path("917409193202@s.whatsapp.net", "DOC-1", str(store)) is None

    def test_get_messages_includes_filename_and_type(self, tmp_path):
        from app.bridge import MessageArchive

        store = _make_stores(tmp_path)
        archive = MessageArchive(str(store / "messages.db"))
        msgs = archive.get_messages("917409193202@s.whatsapp.net")
        by_id = {m.id: m for m in msgs}
        assert by_id["IMG-1"].media_type == "image"
        assert by_id["IMG-1"].filename == "image_20260826_010101_ABC123.jpg"


@pytest.mark.asyncio
async def test_media_endpoint_serves_file_with_query_token(tmp_path):
    from httpx import ASGITransport, AsyncClient

    import app.main as main_mod

    from .conftest import build_container

    store = _make_stores(Path(tmp_path) / "st")
    from app.bridge import MessageArchive

    container = build_container(
        tmp_path,
        [],
        archive=MessageArchive(str(store / "messages.db")),
        whatsapp_store_dir=str(store),
    )
    main_mod.set_container(container)
    token = container.settings.service_token
    async with AsyncClient(transport=ASGITransport(app=main_mod.app), base_url="http://t") as ac:
        ok = await ac.get(
            "/agents/whatsapp/media",
            params={
                "chat_jid": "917409193202@s.whatsapp.net",
                "message_id": "IMG-1",
                "token": token,
            },
        )
        assert ok.status_code == 200
        assert ok.headers["content-type"].startswith("image/jpeg")
        assert ok.content.startswith(b"\xff\xd8")

        missing = await ac.get(
            "/agents/whatsapp/media",
            params={
                "chat_jid": "917409193202@s.whatsapp.net",
                "message_id": "DOC-1",
                "token": token,
            },
        )
        assert missing.status_code == 404  # FakeBridge has no download; graceful

        unauth = await ac.get(
            "/agents/whatsapp/media",
            params={"chat_jid": "917409193202@s.whatsapp.net", "message_id": "IMG-1"},
        )
        assert unauth.status_code == 401


@pytest.mark.asyncio
async def test_chat_messages_flags_and_reactions_split(tmp_path):
    """chat_messages: resolvable flag per media row; reactions separated."""
    from httpx import ASGITransport, AsyncClient

    import app.main as main_mod

    from .conftest import auth_headers, build_container

    store = _make_stores(Path(tmp_path) / "st")
    conn = sqlite3.connect(store / "messages.db")
    conn.execute(
        "INSERT INTO messages VALUES ('REACTION-1', '917409193202@s.whatsapp.net',"
        " '917409193202', '👍', 3, 0, 'reaction', NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, NULL)"
    )
    conn.commit()
    conn.close()

    from app.bridge import MessageArchive

    container = build_container(
        tmp_path,
        [],
        archive=MessageArchive(str(store / "messages.db")),
        whatsapp_store_dir=str(store),
    )
    main_mod.set_container(container)
    async with AsyncClient(transport=ASGITransport(app=main_mod.app), base_url="http://t") as ac:
        resp = await ac.get(
            "/agents/whatsapp/chats/messages",
            params={"chat_jid": "917409193202@s.whatsapp.net"},
            headers=auth_headers(),
        )
    body = resp.json()
    by_id = {m["id"]: m for m in body["messages"]}
    assert by_id["IMG-1"]["resolvable"] is True
    assert by_id["DOC-1"]["resolvable"] is False
    assert all(m["type"] != "reaction" for m in body["messages"])
    assert len(body["reactions"]) == 1
    assert body["reactions"][0]["content"] == "👍"


@pytest.mark.asyncio
async def test_avatar_endpoint_photo_and_fallback(tmp_path):
    from httpx import ASGITransport, AsyncClient

    import app.main as main_mod

    from .conftest import build_container

    container = build_container(tmp_path, [])
    container.bridge.avatar_bytes = b"\xff\xd8\xff\xe0fake"
    main_mod.set_container(container)
    token = container.settings.service_token
    async with AsyncClient(transport=ASGITransport(app=main_mod.app), base_url="http://t") as ac:
        ok = await ac.get(
            "/agents/whatsapp/avatar",
            params={"jid": "917409193202@s.whatsapp.net", "token": token},
        )
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "image/jpeg"

        container.bridge.avatar_bytes = None
        none = await ac.get(
            "/agents/whatsapp/avatar",
            params={"jid": "99999999999@s.whatsapp.net", "token": token},
        )
        assert none.status_code == 404

        unauth = await ac.get(
            "/agents/whatsapp/avatar",
            params={"jid": "917409193202@s.whatsapp.net"},
        )
        assert unauth.status_code == 401


@pytest.mark.asyncio
async def test_socketio_live_updates(tmp_path, monkeypatch):
    """Socket.IO: token auth, audit push, webhook -> incoming push."""
    import asyncio

    import socketio
    from httpx import ASGITransport, AsyncClient

    import app.main as main_mod

    from .conftest import build_container

    container = build_container(tmp_path, [])
    main_mod.wire_event_bridge(container)
    main_mod.set_container(container)

    # 1. Bad token must be refused by connect handler
    refused = False
    try:
        await main_mod.connect("sid-1", {}, {"token": "wrong"})
    except socketio.exceptions.ConnectionRefusedError:
        refused = True
    assert refused

    # 2. Good token connects and enters "ui" room
    rooms = []

    async def mock_enter_room(sid, room):
        rooms.append((sid, room))

    monkeypatch.setattr(main_mod.sio, "enter_room", mock_enter_room)
    await main_mod.connect("sid-1", {}, {"token": container.settings.service_token})
    assert ("sid-1", "ui") in rooms

    # 3. Chat subscription
    await main_mod.subscribe_chat("sid-1", {"jid": "123@s.whatsapp.net"})
    assert ("sid-1", "chat:123@s.whatsapp.net") in rooms

    # 4. Audit push & Webhook push via emit_live
    emitted = []

    async def mock_emit(event, data, room="ui"):
        emitted.append((event, data, room))

    monkeypatch.setattr(main_mod.sio, "emit", mock_emit)

    # Audit hook emits "audit"
    container.store.audit("action_created", user_id="t", probe=1)
    await asyncio.sleep(0.05)
    audit_events = [d for ev, d, r in emitted if ev == "audit"]
    assert len(audit_events) > 0
    assert audit_events[0]["event"] == "action_created"

    # Webhook emits "incoming"
    async with AsyncClient(transport=ASGITransport(app=main_mod.app), base_url="http://t") as rest:
        unauth = await rest.post("/internal/webhook", json={"chatJID": "x"})
        assert unauth.status_code == 401

        await rest.post(
            "/internal/webhook",
            json={"chatJID": "917409193202@s.whatsapp.net", "eventType": "message"},
            headers={"X-Bridge-Token": container.settings.bridge_token},
        )
        await asyncio.sleep(0.05)
        incoming_events = [d for ev, d, r in emitted if ev == "incoming"]
        assert len(incoming_events) > 0
        assert incoming_events[0]["chat_jid"] == "917409193202@s.whatsapp.net"


@pytest.mark.asyncio
async def test_events_sse_removed(tmp_path):
    """/events SSE route is gone (replaced by Socket.IO)."""
    from httpx import ASGITransport, AsyncClient

    import app.main as main_mod

    from .conftest import build_container

    container = build_container(tmp_path, [])
    main_mod.set_container(container)
    async with AsyncClient(transport=ASGITransport(app=main_mod.app), base_url="http://t") as ac:
        r = await ac.get("/agents/whatsapp/events", timeout=5)
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_react_sends_via_bridge_and_persists(tmp_path):
    """Reaction endpoint: bridge called with correct author, state persisted."""
    from httpx import ASGITransport, AsyncClient

    import app.main as main_mod

    from .conftest import auth_headers, build_container

    store = _make_stores(Path(tmp_path) / "st2")
    from app.bridge import MessageArchive

    container = build_container(
        tmp_path,
        [],
        archive=MessageArchive(str(store / "messages.db")),
        whatsapp_store_dir=str(store),
    )
    main_mod.set_container(container)

    async with AsyncClient(
        transport=ASGITransport(app=main_mod.app), base_url="http://t"
    ) as ac:
        # react to a message sent by the OTHER person (from_me=false path)
        r = await ac.post(
            "/agents/whatsapp/react",
            headers=auth_headers(),
            json={"chat_jid": "917409193202@s.whatsapp.net",
                  "message_id": "DOC-1", "emoji": "👍"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "emoji": "👍"}
        call = container.bridge.reacted[-1]
        assert call["from_me"] is False          # DOC-1 authored by them
        assert call["sender_jid"] == "917409193202@s.whatsapp.net"  # normalized to full JID

        # persisted -> chat_messages carries my_reaction
        msgs = await ac.get(
            "/agents/whatsapp/chats/messages",
            params={"chat_jid": "917409193202@s.whatsapp.net"},
            headers=auth_headers(),
        )
        by_id = {m["id"]: m for m in msgs.json()["messages"]}
        assert by_id["DOC-1"]["my_reaction"] == "👍"

        # toggle same emoji off -> bridge receives empty emoji + record cleared
        r2 = await ac.post(
            "/agents/whatsapp/react",
            headers=auth_headers(),
            json={"chat_jid": "917409193202@s.whatsapp.net",
                  "message_id": "DOC-1", "emoji": ""},
        )
        assert r2.status_code == 200
        assert container.bridge.reacted[-1]["emoji"] == ""
        assert by_id.clear() or True
        msgs2 = await ac.get(
            "/agents/whatsapp/chats/messages",
            params={"chat_jid": "917409193202@s.whatsapp.net"},
            headers=auth_headers(),
        )
        by_id2 = {m["id"]: m for m in msgs2.json()["messages"]}
        assert by_id2["DOC-1"]["my_reaction"] is None


@pytest.mark.asyncio
async def test_react_own_message_uses_from_me(tmp_path):
    from httpx import ASGITransport, AsyncClient

    import app.main as main_mod

    from .conftest import auth_headers, build_container

    store = _make_stores(Path(tmp_path) / "st3")
    conn = sqlite3.connect(store / "messages.db")
    conn.execute(
        "INSERT INTO messages VALUES ('MINE-1', '917409193202@s.whatsapp.net',"
        " '919863098661', '', 5, 1, 'text', NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, NULL)"
    )
    conn.commit()
    conn.close()

    from app.bridge import MessageArchive

    container = build_container(
        tmp_path,
        [],
        archive=MessageArchive(str(store / "messages.db")),
        whatsapp_store_dir=str(store),
    )
    main_mod.set_container(container)
    async with AsyncClient(
        transport=ASGITransport(app=main_mod.app), base_url="http://t"
    ) as ac:
        r = await ac.post(
            "/agents/whatsapp/react",
            headers=auth_headers(),
            json={"chat_jid": "917409193202@s.whatsapp.net",
                  "message_id": "MINE-1", "emoji": "🔥"},
        )
    assert r.status_code == 200
    call = container.bridge.reacted[-1]
    assert call["from_me"] is True and call["sender_jid"] is None


@pytest.mark.asyncio
async def test_react_bridge_failure_passthrough(tmp_path):
    from httpx import ASGITransport, AsyncClient

    import app.main as main_mod

    from .conftest import auth_headers, build_container

    store = _make_stores(Path(tmp_path) / "st4")
    from app.bridge import BridgeError, MessageArchive

    container = build_container(
        tmp_path,
        [],
        archive=MessageArchive(str(store / "messages.db")),
        whatsapp_store_dir=str(store),
    )

    async def fail_react(*a, **k):
        raise BridgeError("reaction failed (500): nope")

    container.bridge.react = fail_react
    main_mod.set_container(container)
    async with AsyncClient(
        transport=ASGITransport(app=main_mod.app), base_url="http://t"
    ) as ac:
        r = await ac.post(
            "/agents/whatsapp/react",
            headers=auth_headers(),
            json={"chat_jid": "917409193202@s.whatsapp.net",
                  "message_id": "DOC-1", "emoji": "❤️"},
        )
        assert r.status_code == 502
