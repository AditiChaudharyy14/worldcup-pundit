"""SQLite persistence via aiosqlite.

processed_events is the idempotency ledger detector.py checks before ever
putting an Event on its queue: INSERT OR IGNORE + rowcount tells us in one
round trip whether this exact event has already been emitted (by a previous
run, or an earlier point in this same run).

users / subscriptions / streaks are scaffolded empty per the phase-2 spec --
no reads/writes against them yet, that's a later phase.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from app.models import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events (
    dedupe_key TEXT PRIMARY KEY,
    fixture_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    ts INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    fixture_id INTEGER,
    event_type TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS streaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    streak_type TEXT,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Storage:
    def __init__(self, path: Path | str):
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> "Storage":
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Storage":
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def try_mark_processed(self, event: Event) -> bool:
        """Atomically records `event` as processed. Returns True the first
        time a given dedupe_key is seen (caller should emit), False on any
        repeat (caller should skip) -- this is what makes replaying the same
        fixture twice a no-op rather than a duplicate-event source.
        """
        assert self._conn is not None, "Storage.connect() not called"
        cursor = await self._conn.execute(
            """
            INSERT OR IGNORE INTO processed_events
                (dedupe_key, fixture_id, event_type, ts, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event.dedupe_key, event.fixture_id, event.type, event.ts, json.dumps(event.payload)),
        )
        await self._conn.commit()
        return cursor.rowcount == 1
