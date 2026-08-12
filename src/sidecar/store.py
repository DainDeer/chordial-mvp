"""the sidecar's own sqlite state - deliberately NOT the app database.

this file lives on the person's machine and holds only what the device
needs to work offline: a kv table (device token, server url), the focus
session log, and the outbox. stdlib sqlite3, WAL mode, one writer - the
sidecar is a single process and this store optimizes for being obvious.

the outbox is the client half of the sync contract (device_sync.py holds
the server half): every derived event gets a client uuid and a per-device
monotonic seq (AUTOINCREMENT - sqlite guarantees it never reuses a rowid,
so the sequence stays monotonic even across deletes). rows at or below the
server's durable ACK cursor are trimmed; everything above it re-sends until
acked. re-linking the device (a NEW device row server-side) resets the
server cursor to zero while our seqs would keep counting - the gap would
pin the new cursor forever - so a handshake with a different device uuid
REBASES the outbox: pending events are renumbered from seq 1.
"""
from __future__ import annotations

import json
import sqlite3
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uuid TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    minutes REAL NOT NULL,
    started_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    outcome TEXT,
    ended_at TEXT
);
"""


class SidecarStore:
    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- kv --------------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def put(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
        self._conn.commit()

    # --- the outbox --------------------------------------------------------

    def enqueue(self, event_type: str, payload: dict,
                occurred_at: Optional[str] = None) -> int:
        """append one derived event; returns its seq."""
        cursor = self._conn.execute(
            "INSERT INTO outbox (event_uuid, event_type, payload, occurred_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid_mod.uuid4()), event_type, json.dumps(payload),
             occurred_at or utc_now_iso()))
        self._conn.commit()
        return int(cursor.lastrowid)

    def pending(self, limit: int = 100) -> list[dict]:
        """the oldest unacked events, in seq order, sync-contract shaped."""
        rows = self._conn.execute(
            "SELECT seq, event_uuid, event_type, payload, occurred_at "
            "FROM outbox ORDER BY seq LIMIT ?", (limit,)).fetchall()
        return [{
            "id": r["event_uuid"],
            "seq": r["seq"],
            "type": r["event_type"],
            "payload": json.loads(r["payload"]),
            "occurred_at": r["occurred_at"],
        } for r in rows]

    def trim(self, acked_seq: int) -> int:
        """drop everything the server has durably acked."""
        cursor = self._conn.execute(
            "DELETE FROM outbox WHERE seq <= ?", (acked_seq,))
        self._conn.commit()
        return cursor.rowcount

    def rebase_outbox(self) -> int:
        """renumber pending events from seq 1 (fresh device row server-side:
        its cursor is 0, and a first event at seq 47 would hold it forever).
        event uuids are kept - if the OLD device somehow already landed one,
        the server's uuid unique-constraint dedupes it."""
        rows = self._conn.execute(
            "SELECT event_uuid, event_type, payload, occurred_at "
            "FROM outbox ORDER BY seq").fetchall()
        self._conn.execute("DELETE FROM outbox")
        # AUTOINCREMENT tracks the high-water mark separately; reset it so
        # the renumbered rows really do start from 1
        self._conn.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'outbox'")
        for r in rows:
            self._conn.execute(
                "INSERT INTO outbox (event_uuid, event_type, payload, "
                "occurred_at) VALUES (?, ?, ?, ?)",
                (r["event_uuid"], r["event_type"], r["payload"],
                 r["occurred_at"]))
        self._conn.commit()
        return len(rows)

    # --- sessions ------------------------------------------------------------

    def insert_session(self, label: Optional[str], minutes: float,
                       started_at: str, ends_at: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO sessions (label, minutes, started_at, ends_at) "
            "VALUES (?, ?, ?, ?)", (label, minutes, started_at, ends_at))
        self._conn.commit()
        return int(cursor.lastrowid)

    def active_session(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE outcome IS NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def close_session(self, session_id: int, outcome: str,
                      ended_at: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET outcome = ?, ended_at = ? WHERE id = ?",
            (outcome, ended_at, session_id))
        self._conn.commit()
