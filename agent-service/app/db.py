"""SQLite storage for pending actions, the audit trail, and rate-limit events.

Single-writer SQLite (WAL) — run the service with one uvicorn worker.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL,
    executed_at REAL,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON pending_actions(status);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_id TEXT,
    event TEXT NOT NULL,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversation_messages(user_id, id);
CREATE TABLE IF NOT EXISTS message_reactions (
    chat_jid TEXT NOT NULL,
    message_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    updated_at REAL,
    PRIMARY KEY (chat_jid, message_id)
);
CREATE TABLE IF NOT EXISTS context_entities (
    user_id TEXT PRIMARY KEY,
    last_contact_jid TEXT,
    last_contact_name TEXT,
    updated_at REAL
);
"""


@dataclass(frozen=True)
class PendingAction:
    id: str
    user_id: str
    tool: str
    args: dict[str, Any]
    reason: str
    warnings: list[str]
    status: str
    created_at: float
    expires_at: float
    decided_at: float | None = None
    executed_at: float | None = None
    result: dict[str, Any] | None = None


def _row_to_action(row: sqlite3.Row) -> PendingAction:
    return PendingAction(
        id=row["id"],
        user_id=row["user_id"],
        tool=row["tool"],
        args=json.loads(row["args_json"]),
        reason=row["reason"],
        warnings=json.loads(row["warnings_json"]),
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        executed_at=row["executed_at"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
    )


class AgentStore:
    def __init__(self, db_path: str):
        # Optional synchronous callback invoked after every audit write;
        # used by the SSE event stream to push live updates to browsers.
        self.audit_hook = None
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        # One shared connection; FastAPI handlers run on one event loop and
        # SQLite calls are fast. Guarded with a lock for safety under threads.
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- Audit ---

    def audit(self, event: str, user_id: str | None = None, **detail: Any) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (ts, user_id, event, detail_json) VALUES (?, ?, ?, ?)",
            (time.time(), user_id, event, json.dumps(detail, default=str)),
        )
        self.conn.commit()
        if self.audit_hook is not None:
            try:
                self.audit_hook(
                    {
                        "type": "audit",
                        "ts": time.time(),
                        "user_id": user_id,
                        "event": event,
                        "detail": detail,
                    }
                )
            except Exception:  # noqa: BLE001 - telemetry must never break writes
                pass

    def get_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ts, user_id, event, detail_json FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ts": r["ts"],
                "user_id": r["user_id"],
                "event": r["event"],
                "detail": json.loads(r["detail_json"]),
            }
            for r in rows
        ]

    # --- Conversation memory ---

    def add_memory(self, user_id: str, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO conversation_messages (user_id, ts, role, content) VALUES (?, ?, ?, ?)",
            (user_id, time.time(), role, content[:2000]),
        )
        # Rolling window: keep the most recent 40 turns per user.
        self.conn.execute(
            "DELETE FROM conversation_messages WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM conversation_messages WHERE user_id = ? ORDER BY id DESC LIMIT 40)",
            (user_id, user_id),
        )
        self.conn.commit()

    def get_memory(self, user_id: str, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT role, content, ts FROM conversation_messages "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in reversed(rows)]

    def set_last_contact(self, user_id: str, jid: str, name: str | None) -> None:
        self.conn.execute(
            "INSERT INTO context_entities (user_id, last_contact_jid, last_contact_name, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "last_contact_jid=excluded.last_contact_jid, "
            "last_contact_name=excluded.last_contact_name, updated_at=excluded.updated_at",
            (user_id, jid, name or "", time.time()),
        )
        self.conn.commit()

    def get_last_contact(self, user_id: str) -> tuple[str | None, str | None]:
        row = self.conn.execute(
            "SELECT last_contact_jid, last_contact_name FROM context_entities WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return (row["last_contact_jid"], row["last_contact_name"]) if row else (None, None)

    def set_my_reaction(self, chat_jid: str, message_id: str, emoji: str) -> None:
        """Record/clear THIS account's reaction on a specific message."""
        if emoji:
            self.conn.execute(
                "INSERT INTO message_reactions (chat_jid, message_id, emoji, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(chat_jid, message_id) DO UPDATE SET "
                "emoji=excluded.emoji, updated_at=excluded.updated_at",
                (chat_jid, message_id, emoji, time.time()),
            )
        else:
            self.conn.execute(
                "DELETE FROM message_reactions WHERE chat_jid = ? AND message_id = ?",
                (chat_jid, message_id),
            )
        self.conn.commit()

    def get_my_reactions(self, chat_jid: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT message_id, emoji FROM message_reactions WHERE chat_jid = ? AND emoji != ''",
            (chat_jid,),
        ).fetchall()
        return {r["message_id"]: r["emoji"] for r in rows}

    # --- Pending actions ---

    def create_action(
        self,
        user_id: str,
        tool: str,
        args: dict[str, Any],
        reason: str,
        warnings: list[str],
        ttl_seconds: float,
    ) -> PendingAction:
        now = time.time()
        action_id = uuid.uuid4().hex
        self.conn.execute(
            """INSERT INTO pending_actions
               (id, user_id, tool, args_json, reason, warnings_json, status, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                action_id,
                user_id,
                tool,
                json.dumps(args),
                reason,
                json.dumps(warnings),
                now,
                now + ttl_seconds,
            ),
        )
        self.conn.commit()
        return self.get_action(action_id)  # type: ignore[return-value]

    def get_action(self, action_id: str) -> PendingAction | None:
        row = self.conn.execute(
            "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return _row_to_action(row) if row else None

    def list_actions(self, status: str | None = None) -> list[PendingAction]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM pending_actions WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pending_actions ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_action(r) for r in rows]

    def expire_stale(self) -> int:
        """Mark overdue pending actions expired; returns count newly expired."""
        cur = self.conn.execute(
            "UPDATE pending_actions SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at <= ?",
            (time.time(),),
        )
        self.conn.commit()
        return cur.rowcount

    def decide_action(self, action_id: str, approved: bool) -> PendingAction | None:
        """Transition a pending action to approved/rejected.

        Returns None when the action does not exist or is no longer pending
        (already decided/expired). Expire-stale runs first so overdue actions
        can never be approved.
        """
        self.expire_stale()
        new_status = "approved" if approved else "rejected"
        cur = self.conn.execute(
            "UPDATE pending_actions SET status = ?, decided_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (new_status, time.time(), action_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_action(action_id)

    def complete_action(
        self, action_id: str, success: bool, result: dict[str, Any]
    ) -> PendingAction | None:
        status = "executed" if success else "failed"
        self.conn.execute(
            "UPDATE pending_actions SET status = ?, executed_at = ?, result_json = ? "
            "WHERE id = ? AND status = 'approved'",
            (status, time.time(), json.dumps(result), action_id),
        )
        self.conn.commit()
        return self.get_action(action_id)
