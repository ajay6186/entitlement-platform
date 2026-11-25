"""Received-webhook history in SQLite (survives restarts when /app/data is a volume). Also the de-duplication record."""
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS received (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    event_id TEXT,
    event_type TEXT,
    outcome TEXT NOT NULL,          -- accepted | duplicate | rejected | simulated_failure
    http_status INTEGER NOT NULL,
    signature_valid INTEGER,        -- 1 / 0 / NULL (not checked)
    reason TEXT,
    headers TEXT NOT NULL,
    body TEXT
);
CREATE INDEX IF NOT EXISTS ix_received_event ON received (event_id, outcome);
"""


class EventStore:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def already_processed(self, event_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM received WHERE event_id = ? AND outcome = 'accepted' LIMIT 1",
                                   (event_id,)).fetchone()
        return row is not None

    def add(self, *, event_id: str | None, event_type: str | None, outcome: str, http_status: int,
            signature_valid: bool | None, reason: str | None, headers: dict, body: str | None) -> dict:
        received_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO received (received_at, event_id, event_type, outcome, http_status, signature_valid,"
                " reason, headers, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (received_at, event_id, event_type, outcome, http_status,
                 None if signature_valid is None else int(signature_valid), reason, json.dumps(headers),
                 body[:4000] if body is not None else None))
            self._db.commit()
            return self._get(cur.lastrowid)

    def _get(self, row_id: int) -> dict:
        return _row(self._db.execute("SELECT * FROM received WHERE id = ?", (row_id,)).fetchone())

    def list(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM received ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT outcome, COUNT(*) FROM received GROUP BY outcome").fetchall()
        out = {k: 0 for k in ("accepted", "duplicate", "rejected", "simulated_failure")}
        out.update({r[0]: r[1] for r in rows})
        return out

    def clear(self) -> int:
        with self._lock:
            n = self._db.execute("DELETE FROM received").rowcount
            self._db.commit()
        return n


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["headers"] = json.loads(d["headers"])
    d["signature_valid"] = None if d["signature_valid"] is None else bool(d["signature_valid"])
    try:
        d["payload"] = json.loads(d.pop("body")) if d.get("body") else None
    except ValueError:
        d["payload"] = None
    return d
