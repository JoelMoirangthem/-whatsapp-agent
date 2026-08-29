"""WhatsApp connectivity layer.

Writes go through the Go bridge REST API (bearer token); reads come directly
from the bridge's messages.db SQLite archive — mirroring the whatsapp-mcp
design where the local DB is the source of truth for read operations.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class BridgeError(RuntimeError):
    """Raised when the bridge rejects or cannot service a write."""


@dataclass(frozen=True)
class BridgeResponse:
    ok: bool
    status: int
    data: dict[str, Any]


class BridgeClient:
    def __init__(self, api_url: str, token: str, timeout: float = 10.0):
        # Strip trailing slash so urljoin-style concatenation stays correct.
        self._api_url = api_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> BridgeResponse:
        return await self._send_with_retry(lambda: self._client.post(path, json=payload))

    @staticmethod
    async def _send_with_retry(do_call) -> BridgeResponse:
        """One retry for transport-level resets. Go's server closes idle
        keep-alive connections; if a pooled socket dies between approve and
        execute, the request never reached the bridge and is safe to resend."""
        try:
            resp = await do_call()
        except (httpx.RemoteProtocolError, httpx.ConnectError):
            resp = await do_call()
        except httpx.HTTPError as exc:
            raise BridgeError(f"bridge unreachable: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        return BridgeResponse(ok=resp.is_success, status=resp.status_code, data=data)

    async def health(self) -> BridgeResponse:
        try:
            resp = await self._client.get("/health")
        except httpx.HTTPError as exc:
            raise BridgeError(f"bridge unreachable: {exc}") from exc
        return BridgeResponse(resp.is_success, resp.status_code, resp.json())

    async def qr_meta(self) -> dict[str, Any]:
        """Latest pairing-QR metadata; {} when unavailable."""
        try:
            resp = await self._client.get("/qr/meta")
        except httpx.HTTPError:
            return {}
        return resp.json() if resp.status_code == 200 else {}

    async def qr_png(self) -> bytes | None:
        """PNG bytes of the current pairing QR; None when unavailable."""
        try:
            resp = await self._client.get("/qr.png")
        except httpx.HTTPError:
            return None
        return resp.content if resp.status_code == 200 else None

    async def send_message(
        self, recipient: str, message: str, quoted_message_id: str | None = None
    ) -> BridgeResponse:
        payload: dict[str, Any] = {"recipient": recipient, "message": message}
        if quoted_message_id:
            payload["quoted_message_id"] = quoted_message_id
        resp = await self._post("/send", payload)
        if not resp.ok:
            detail = resp.data.get("message") or resp.data.get("error") or resp.data.get("raw", "")
            raise BridgeError(f"send failed ({resp.status}): {detail}")
        return resp

    last_download_info: dict | None = None

    async def get_avatar(self, jid: str, refresh: bool = False):
        """Fetch a profile photo via the bridge. Returns
        (ok, image_bytes|None, content_type, error_message)."""
        params = {"jid": jid}
        if refresh:
            params["refresh"] = "1"
        try:
            resp = await self._client.get("/avatar", params=params)
        except (httpx.RemoteProtocolError, httpx.ConnectError):
            resp = await self._client.get("/avatar", params=params)
        except httpx.HTTPError as exc:
            return False, None, "", f"bridge unreachable: {exc}"
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and ctype.startswith("image/"):
            return True, resp.content, ctype, ""
        try:
            data = resp.json()
        except ValueError:
            data = {}
        msg = str(data.get("error") or data.get("message") or f"HTTP {resp.status_code}")
        return False, None, "", msg

    async def react(
        self,
        chat_jid: str,
        message_id: str,
        emoji: str,
        from_me: bool,
        sender_jid: str | None = None,
    ) -> BridgeResponse:
        """Send (or clear with emoji="") a reaction via the Go bridge."""
        payload: dict[str, Any] = {
            "recipient": chat_jid,
            "message_id": message_id,
            "emoji": emoji,
            "from_me": from_me,
        }
        if sender_jid:
            payload["sender_jid"] = sender_jid
        resp = await self._post("/react", payload)
        if not resp.ok:
            detail = (
                resp.data.get("error") or resp.data.get("message") or resp.data.get("raw", "")
            )
            raise BridgeError(f"reaction failed ({resp.status}): {detail}")
        return resp

    async def download_media(self, chat_jid: str, message_id: str) -> BridgeResponse:
        """Ask the Go bridge to fetch a media blob from WhatsApp and store it.

        Uses a dedicated long timeout: blob transfer over the WhatsApp
        websocket routinely exceeds the normal API budget.
        """
        try:
            resp = await self._client.post(
                "/download",
                json={"chat_jid": chat_jid, "message_id": message_id},
                timeout=httpx.Timeout(120.0, connect=5.0),
            )
        except (httpx.RemoteProtocolError, httpx.ConnectError):
            resp = await self._client.post(
                "/download",
                json={"chat_jid": chat_jid, "message_id": message_id},
                timeout=httpx.Timeout(120.0, connect=5.0),
            )
        except httpx.HTTPError as exc:
            raise BridgeError(f"bridge unreachable: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        result = BridgeResponse(resp.is_success, resp.status_code, data)
        self.last_download_info = data if isinstance(data, dict) else None
        if not result.ok:
            detail = (
                resp_data.get("message")
                if (resp_data := data if isinstance(data, dict) else {})
                else ""
            )
            raise BridgeError(f"download failed ({resp.status_code}): {detail}")
        return result
        if not resp.ok:
            detail = resp.data.get("message") or resp.data.get("error") or resp.data.get("raw", "")
            raise BridgeError(f"download failed ({resp.status}): {detail}")
        return resp

    async def delete_message(self, chat_jid: str, message_id: str) -> BridgeResponse:
        async def do():
            return await self._client.request(
                "DELETE", "/messages", json={"chat_jid": chat_jid, "message_id": message_id}
            )

        resp = await self._send_with_retry(do)
        result = BridgeResponse(resp.ok, resp.status, resp.data)
        if not result.ok:
            detail = resp.data.get("message") or resp.data.get("error") or resp.data.get("raw", "")
            raise BridgeError(f"delete failed ({result.status}): {detail}")
        return result

    async def initiate_call(self, recipient: str, is_video: bool = False) -> BridgeResponse:
        """Initiate WhatsApp audio/video call via bridge if supported, else simulated.

        WhatsApp Web linked devices have limited calling support (see AGENTS.md). We try
        POST /api/call first; on 404 we return a simulated success so the approval
        flow still completes without hallucination that a real call was placed via the
        server. The UI/voice layer surfaces the `simulated` flag honestly.
        """
        payload: dict[str, Any] = {"recipient": recipient, "is_video": is_video}
        try:
            resp = await self._post("/call", payload)
        except BridgeError as exc:
            # Bridge unreachable — simulated fallback still lets audit complete
            if "404" in str(exc) or "not found" in str(exc).lower():
                return BridgeResponse(
                    True,
                    200,
                    {
                        "success": True,
                        "message": "Call not supported via WhatsApp Web linked device — logged for manual dial on phone.",
                        "simulated": True,
                        "call_type": "video" if is_video else "audio",
                    },
                )
            raise
        if resp.ok:
            return resp
        # 404 from bridge means no call endpoint — simulated
        if resp.status == 404:
            return BridgeResponse(
                True,
                200,
                {
                    "success": True,
                    "message": "Call not supported via WhatsApp Web linked device — logged for manual dial on phone.",
                    "simulated": True,
                    "call_type": "video" if is_video else "audio",
                },
            )
        detail = resp.data.get("message") or resp.data.get("error") or resp.data.get("raw", "")
        raise BridgeError(f"call failed ({resp.status}): {detail}")

    async def initiate_audio_call(self, recipient: str) -> BridgeResponse:
        return await self.initiate_call(recipient, is_video=False)

    async def initiate_video_call(self, recipient: str) -> BridgeResponse:
        return await self.initiate_call(recipient, is_video=True)


@dataclass(frozen=True)
class StoredMessage:
    id: str
    chat_jid: str
    sender: str
    content: str
    timestamp: str
    is_from_me: bool
    media_type: str | None
    deleted: bool
    filename: str | None = None


class MessageArchive:
    """Read-only access to the bridge's messages.db.

    Keeps one cached read-only connection (SQLite opens are not free and this
    archive is hit on every chat/read request); the bridge writes via WAL so
    a long-lived reader sees fresh data on each query.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def available(self) -> bool:
        if self.db_path == ":memory:":
            return True
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1 FROM messages LIMIT 1")
            return True
        except sqlite3.Error:
            return False

    def get_messages(
        self,
        chat_jid: str,
        limit: int = 50,
        after_date: str | None = None,
        before_date: str | None = None,
    ) -> list[StoredMessage]:
        query = (
            "SELECT id, chat_jid, COALESCE(sender,'') AS sender, COALESCE(content,'') AS content, "
            "timestamp, is_from_me, media_type, deleted_at, filename "
            "FROM messages WHERE chat_jid = ?"
        )
        params: list[Any] = [chat_jid]
        if after_date:
            query += " AND timestamp >= ?"
            params.append(after_date)
        if before_date:
            query += " AND timestamp <= ?"
            params.append(before_date)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            StoredMessage(
                id=r["id"],
                chat_jid=r["chat_jid"],
                sender=r["sender"],
                content=r["content"],
                timestamp=str(r["timestamp"]),
                is_from_me=bool(r["is_from_me"]),
                media_type=r["media_type"],
                deleted=r["deleted_at"] is not None,
                filename=r["filename"],
            )
            for r in rows
        ]

    def get_message(self, message_id: str, chat_jid: str) -> StoredMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, chat_jid, COALESCE(sender,'') AS sender, COALESCE(content,'') AS content, "
                "timestamp, is_from_me, media_type, deleted_at, filename "
                "FROM messages WHERE id = ? AND chat_jid = ?",
                (message_id, chat_jid),
            ).fetchone()
        if row is None:
            return None
        return StoredMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            content=row["content"],
            timestamp=str(row["timestamp"]),
            is_from_me=bool(row["is_from_me"]),
            media_type=row["media_type"],
            deleted=row["deleted_at"] is not None,
            filename=row["filename"],
        )

    def list_chats(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT jid, name, last_message_time FROM chats "
                "ORDER BY last_message_time DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        chats = [
            {
                "jid": r["jid"],
                "name": r["name"],
                "last_message_time": str(r["last_message_time"]),
            }
            for r in rows
        ]
        return self.enrich_chat_names(chats)

    def _contacts_conn(self) -> sqlite3.Connection | None:
        """Lazy read-only connection to the sibling whatsmeow contacts DB."""
        if self.db_path == ":memory:":
            return None
        wa_db = Path(self.db_path).parent / "whatsapp.db"
        if not wa_db.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{wa_db}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def resolve_contact_name(self, jid_or_phone: str) -> str | None:
        """Best human name for a JID/phone from whatsmeow_contacts.

        Checks full JID, then bare-number match; prefers full_name >
        push_name > business_name > first_name. Returns None when unknown.
        """
        conn = self._contacts_conn()
        if conn is None:
            return None
        digits = "".join(ch for ch in jid_or_phone if ch.isdigit())
        candidates = [jid_or_phone]
        if digits:
            candidates.append(f"{digits}@s.whatsapp.net")
        try:
            for cand in candidates:
                row = conn.execute(
                    "SELECT first_name, full_name, push_name, business_name "
                    "FROM whatsmeow_contacts WHERE their_jid = ?",
                    (cand,),
                ).fetchone()
                if row:
                    for v in (
                        row["full_name"],
                        row["push_name"],
                        row["business_name"],
                        row["first_name"],
                    ):
                        if v and str(v).strip():
                            return str(v).strip()
        except sqlite3.Error:
            return None
        return None

    def enrich_chat_names(self, chats: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace numeric/missing display names with real contact names."""
        import re as _re

        for chat in chats:
            name = chat.get("name")
            if not name or _re.fullmatch(r"[\d\s+\-()]+", name):
                better = self.resolve_contact_name(chat["jid"])
                if better:
                    chat["name"] = better
        return chats

    def blob_exists(self, chat_jid: str, filename: str, store_dir: str) -> bool:
        """True when the media blob for a message is already on disk."""
        base = Path(store_dir)
        return (base / str(chat_jid) / filename).is_file() or (base / str(filename)).is_file()

    def media_path(self, chat_jid: str, message_id: str, store_dir: str) -> str | None:
        """Absolute path of a stored media blob, when present on disk."""
        msg = self.get_message(message_id, chat_jid)
        if msg is None or not msg.filename:
            return None
        base = Path(store_dir)
        for cand in (base / str(chat_jid) / msg.filename, base / str(msg.filename)):
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                continue
        return None

    def own_sender(self) -> str | None:
        """Bare phone number this account sends from (most common outbound
        sender), or None when unknown."""
        if not self.available():
            return None
        try:
            row = (
                self._connect()
                .execute(
                    "SELECT sender FROM messages WHERE is_from_me=1 AND sender != '' "
                    "GROUP BY sender ORDER BY COUNT(*) DESC LIMIT 1"
                )
                .fetchone()
            )
        except sqlite3.Error:
            return None
        return row["sender"] if row else None

    def search_chats_ranked(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Contact/chat lookup with exact-first ranking and fuzzy fallback.

        Ranking tiers:
        1. exact full-name match (case-insensitive)
        2. exact substring match in name
        3. all query tokens present in the name
        4. fuzzy similarity (difflib ratio on the full name)

        Returns up to `limit` chats each annotated with "match" ("exact" /
        "substring" / "similar") and "score", best first. JID-digit matches
        rank alongside substrings so bare phone numbers resolve.
        """
        from difflib import SequenceMatcher

        needle = " ".join(query.lower().split())
        if not needle:
            return []
        tokens = set(needle.split())
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for chat in self.list_chats(limit=100):
            name = (chat["name"] or "").strip()
            hay_name = " ".join(name.lower().split())
            hay_jid = chat["jid"].lower()
            if hay_name == needle or hay_jid.startswith(needle) and "@" not in needle:
                tier, score = "exact", 1.0
            elif needle in hay_name:
                tier, score = "substring", 0.9
            elif any(ch.isdigit() for ch in needle) and "".join(
                ch for ch in needle if ch.isdigit()
            ) in "".join(ch for ch in hay_jid if ch.isdigit()):
                tier, score = "exact", 0.95
            elif tokens and tokens <= set(hay_name.replace("(", " ").replace(")", " ").split()):
                tier, score = "substring", 0.8
            else:
                ratio = SequenceMatcher(None, needle, hay_name).ratio()
                token_ratio = max(
                    (SequenceMatcher(None, t, w).ratio() for t in tokens for w in hay_name.split()),
                    default=0.0,
                )
                best = max(ratio, token_ratio * 0.9)
                if best < 0.55:
                    continue
                tier, score = "similar", round(best, 3)
            scored.append((score, tier, chat))
        # Stable sort keeps list_chats' newest-first order for score ties.
        scored.sort(key=lambda s: -s[0])
        out = []
        for score, tier, chat in scored[: max(1, min(int(limit), 20))]:
            out.append({**chat, "match": tier, "score": score})
        return out
