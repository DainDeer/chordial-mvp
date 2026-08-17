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
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    label TEXT,
    target_minutes REAL NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    seconds INTEGER,
    end_reason TEXT
);
CREATE TABLE IF NOT EXISTS offers (
    offer_uuid TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    returned_at TEXT,
    candidates TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    excised_start TEXT,
    excised_end TEXT
);
"""

# columns added to existing tables after the CREATE IF NOT EXISTS era -
# executescript can't grow a table, so each is checked and ALTERed in.
# rewind (docs/REWIND_DESIGN.md section 6): a run with an unresolved
# correction freezes instead of banking; frozen_at stops its clock,
# frozen_reason remembers which transition was intended.
_COLUMN_MIGRATIONS = [
    ("runs", "frozen_at", "TEXT"),
    ("runs", "frozen_reason", "TEXT"),
]


class SidecarStore:
    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        for table, column, kind in _COLUMN_MIGRATIONS:
            present = {r["name"] for r in self._conn.execute(
                f"PRAGMA table_info({table})")}
            if column not in present:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
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
        return self._renumber_above(0)

    def align_seq(self, acked_seq: int) -> int:
        """make every seq - pending rows AND the autoincrement high-water -
        sit ABOVE the server's durable cursor. the inverse hazard of rebase:
        a fresh sidecar db under an EXISTING device restarts numbering at 1
        while the server cursor is far ahead, and everything at or below it
        would be swallowed as a duplicate (acked, trimmed, silently lost)."""
        low = self._conn.execute(
            "SELECT MIN(seq) AS lo FROM outbox").fetchone()["lo"]
        high_water = self._conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'outbox'"
        ).fetchone()
        current_top = int(high_water["seq"]) if high_water else 0
        if (low is None or low > acked_seq) and current_top >= acked_seq:
            return 0
        return self._renumber_above(acked_seq)

    def _renumber_above(self, floor: int) -> int:
        """rewrite the outbox so pending events occupy floor+1..floor+n and
        the next enqueue lands after them. uuids are kept - identity for the
        server's dedup is the uuid, seq is only the ordering contract."""
        rows = self._conn.execute(
            "SELECT event_uuid, event_type, payload, occurred_at "
            "FROM outbox ORDER BY seq").fetchall()
        self._conn.execute("DELETE FROM outbox")
        self._conn.execute(
            "DELETE FROM sqlite_sequence WHERE name = 'outbox'")
        if floor > 0:
            # seed the high-water mark so AUTOINCREMENT continues above it
            self._conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) "
                "VALUES ('outbox', ?)", (floor,))
        for r in rows:
            self._conn.execute(
                "INSERT INTO outbox (event_uuid, event_type, payload, "
                "occurred_at) VALUES (?, ?, ?, ?)",
                (r["event_uuid"], r["event_type"], r["payload"],
                 r["occurred_at"]))
        self._conn.commit()
        return len(rows)

    # --- focus runs -----------------------------------------------------------

    def insert_run(self, task_id: Optional[int], label: Optional[str],
                   target_minutes: float, started_at: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO runs (task_id, label, target_minutes, started_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, label, target_minutes, started_at))
        self._conn.commit()
        return int(cursor.lastrowid)

    def active_run(self) -> Optional[dict]:
        """the RUNNING run: unbanked and not frozen. a frozen run is
        stopped time waiting on its question - it is not active."""
        row = self._conn.execute(
            "SELECT * FROM runs WHERE ended_at IS NULL "
            "AND frozen_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def run(self, run_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def frozen_runs(self) -> list[dict]:
        """unbanked runs whose clock was stopped by an unresolved
        correction - each is waiting for its answer, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE ended_at IS NULL "
            "AND frozen_at IS NOT NULL ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def freeze_run(self, run_id: int, frozen_at: str, reason: str) -> None:
        """stop the clock without banking: the transition happened, the
        question is still open (docs/REWIND_DESIGN.md section 6)."""
        self._conn.execute(
            "UPDATE runs SET frozen_at = ?, frozen_reason = ? WHERE id = ?",
            (frozen_at, reason, run_id))
        self._conn.commit()

    def close_run(self, run_id: int, ended_at: str, seconds: int,
                  end_reason: str) -> None:
        self._conn.execute(
            "UPDATE runs SET ended_at = ?, seconds = ?, end_reason = ? "
            "WHERE id = ?", (ended_at, seconds, end_reason, run_id))
        self._conn.commit()

    # --- rewind offers ---------------------------------------------------------
    # one row per correction question (docs/REWIND_DESIGN.md section 6).
    # candidates/resolution are json; the applied excision interval sits in
    # its own columns so credited-time arithmetic reads it without parsing.

    def insert_offer(self, offer_uuid: str, run_id: int, created_at: str,
                     candidates: list[dict]) -> None:
        self._conn.execute(
            "INSERT INTO offers (offer_uuid, run_id, created_at, candidates) "
            "VALUES (?, ?, ?, ?)",
            (offer_uuid, run_id, created_at, json.dumps(candidates)))
        self._conn.commit()

    def get_offer(self, offer_uuid: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM offers WHERE offer_uuid = ?",
            (offer_uuid,)).fetchone()
        return self._offer_dict(row) if row else None

    def open_offer_for_run(self, run_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM offers WHERE run_id = ? AND status = 'open' "
            "ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
        return self._offer_dict(row) if row else None

    def newest_open_offer(self) -> Optional[dict]:
        """the question currently worth surfacing (at most one per run;
        across runs the newest wins the chip)."""
        row = self._conn.execute(
            "SELECT * FROM offers WHERE status = 'open' "
            "ORDER BY rowid DESC LIMIT 1").fetchone()
        return self._offer_dict(row) if row else None

    def extend_offer(self, offer_uuid: str, candidates: list[dict]) -> None:
        """a further drift while the question is open: new candidates over
        the whole tail, the return forgotten (the person is away again)."""
        self._conn.execute(
            "UPDATE offers SET candidates = ?, returned_at = NULL "
            "WHERE offer_uuid = ?", (json.dumps(candidates), offer_uuid))
        self._conn.commit()

    def mark_offer_returned(self, offer_uuid: str, returned_at: str) -> None:
        self._conn.execute(
            "UPDATE offers SET returned_at = ? WHERE offer_uuid = ?",
            (returned_at, offer_uuid))
        self._conn.commit()

    def resolve_offer(self, offer_uuid: str, status: str, resolution: dict,
                      excised_start: Optional[str] = None,
                      excised_end: Optional[str] = None) -> None:
        self._conn.execute(
            "UPDATE offers SET status = ?, resolution = ?, "
            "excised_start = ?, excised_end = ? WHERE offer_uuid = ?",
            (status, json.dumps(resolution), excised_start, excised_end,
             offer_uuid))
        self._conn.commit()

    def reopen_offer(self, offer_uuid: str) -> None:
        """undo: the interval is lifted and the question comes back - the
        tap may have been the mistake."""
        self._conn.execute(
            "UPDATE offers SET status = 'open', resolution = NULL, "
            "excised_start = NULL, excised_end = NULL WHERE offer_uuid = ?",
            (offer_uuid,))
        self._conn.commit()

    def excised_seconds(self, run_id: int) -> int:
        """the run's total applied excision - intervals are disjoint by
        construction (one open offer per run; a new drift needs new
        activity after the last resolution), so a plain sum is the union."""
        rows = self._conn.execute(
            "SELECT excised_start, excised_end FROM offers "
            "WHERE run_id = ? AND status = 'applied' "
            "AND excised_start IS NOT NULL", (run_id,)).fetchall()
        total = 0.0
        for r in rows:
            start = datetime.fromisoformat(r["excised_start"])
            end = datetime.fromisoformat(r["excised_end"])
            total += max(0.0, (end - start).total_seconds())
        return int(total)

    @staticmethod
    def _offer_dict(row) -> dict:
        offer = dict(row)
        offer["candidates"] = json.loads(offer["candidates"])
        if offer.get("resolution"):
            offer["resolution"] = json.loads(offer["resolution"])
        return offer

    def banked_since(self, since_iso: str) -> dict[int, int]:
        """closed-run seconds per task since `since` (the local day start) -
        the task list's little fill bars. keyed by task_id; untasked runs
        land under 0."""
        rows = self._conn.execute(
            "SELECT COALESCE(task_id, 0) AS task_id, "
            "COALESCE(SUM(seconds), 0) AS total FROM runs "
            "WHERE ended_at IS NOT NULL AND started_at >= ? "
            "GROUP BY COALESCE(task_id, 0)", (since_iso,)).fetchall()
        return {int(r["task_id"]): int(r["total"]) for r in rows}
