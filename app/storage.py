"""SQLite persistence via aiosqlite.

processed_events is the idempotency ledger detector.py checks before ever
putting an Event on its queue: INSERT OR IGNORE + rowcount tells us in one
round trip whether this exact event has already been emitted (by a previous
run, or an earlier point in this same run).

users / subscriptions / streaks were scaffolded empty in phase 2; phase 4
(the Discord bot -- see bot.py/handlers.py/game.py) is the first thing to
actually read/write them, so this module now owns the language prefs,
fixture-follow list, and Hi-Lo streak game state.
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
    external_id TEXT UNIQUE NOT NULL,
    lang TEXT NOT NULL DEFAULT 'en',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    fixture_id INTEGER NOT NULL,
    event_type TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, fixture_id)
);

CREATE TABLE IF NOT EXISTS streaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    streak_type TEXT NOT NULL DEFAULT 'hilo',
    count INTEGER NOT NULL DEFAULT 0,
    best_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, streak_type)
);

CREATE TABLE IF NOT EXISTS hilo_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id INTEGER NOT NULL,
    period TEXT NOT NULL,
    line REAL NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(fixture_id, period)
);

CREATE TABLE IF NOT EXISTS hilo_guesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES hilo_questions(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    guess TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(question_id, user_id)
);
"""


class Storage:
    def __init__(self, path: Path | str):
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> "Storage":
        self._conn = await aiosqlite.connect(self._path)
        await self._migrate_legacy_scaffold()
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        return self

    async def _migrate_legacy_scaffold(self) -> None:
        """The phase-2 users/subscriptions/streaks tables predate the columns
        and constraints the bot needs. They were never written to (no code
        path did so before phase 4), so it's safe to drop and let _SCHEMA
        recreate them rather than hand-write an ALTER TABLE path.
        """
        assert self._conn is not None
        cursor = await self._conn.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in await cursor.fetchall()}
        if columns and "lang" not in columns:
            for table in ("hilo_guesses", "hilo_questions", "streaks", "subscriptions", "users"):
                await self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            await self._conn.commit()

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

    # -- users / language prefs -------------------------------------------

    async def _get_or_create_user_id(self, external_id: str) -> int:
        assert self._conn is not None
        await self._conn.execute("INSERT OR IGNORE INTO users (external_id) VALUES (?)", (external_id,))
        await self._conn.commit()
        cursor = await self._conn.execute("SELECT id FROM users WHERE external_id = ?", (external_id,))
        row = await cursor.fetchone()
        assert row is not None
        return row[0]

    async def get_user_lang(self, external_id: str) -> str:
        assert self._conn is not None
        await self._get_or_create_user_id(external_id)
        cursor = await self._conn.execute("SELECT lang FROM users WHERE external_id = ?", (external_id,))
        row = await cursor.fetchone()
        assert row is not None
        return row[0]

    async def set_user_lang(self, external_id: str, lang: str) -> None:
        assert self._conn is not None
        await self._get_or_create_user_id(external_id)
        await self._conn.execute("UPDATE users SET lang = ? WHERE external_id = ?", (lang, external_id))
        await self._conn.commit()

    # -- fixture follow/unfollow --------------------------------------------

    async def toggle_subscription(self, external_id: str, fixture_id: int) -> bool:
        """Follows `fixture_id` if not already followed, else unfollows.
        Returns True if the user is now following, False if now unfollowed.
        """
        assert self._conn is not None
        user_id = await self._get_or_create_user_id(external_id)
        cursor = await self._conn.execute(
            "SELECT id FROM subscriptions WHERE user_id = ? AND fixture_id = ?", (user_id, fixture_id)
        )
        row = await cursor.fetchone()
        if row is None:
            await self._conn.execute(
                "INSERT INTO subscriptions (user_id, fixture_id) VALUES (?, ?)", (user_id, fixture_id)
            )
            await self._conn.commit()
            return True
        await self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (row[0],))
        await self._conn.commit()
        return False

    async def is_subscribed(self, external_id: str, fixture_id: int) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT 1 FROM subscriptions
            JOIN users ON users.id = subscriptions.user_id
            WHERE users.external_id = ? AND subscriptions.fixture_id = ?
            """,
            (external_id, fixture_id),
        )
        return await cursor.fetchone() is not None

    async def get_active_langs(self, fixture_id: int) -> set[str]:
        """Distinct languages of users currently following `fixture_id`."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT DISTINCT users.lang FROM subscriptions
            JOIN users ON users.id = subscriptions.user_id
            WHERE subscriptions.fixture_id = ?
            """,
            (fixture_id,),
        )
        return {row[0] for row in await cursor.fetchall()}

    # -- Hi-Lo streak game ---------------------------------------------------

    async def create_question(
        self, fixture_id: int, period: str, line: float, channel_id: int
    ) -> int | None:
        """Creates the (fixture_id, period) question. Returns its id, or None
        if one already exists (idempotent against replayed/duplicate events).
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT OR IGNORE INTO hilo_questions (fixture_id, period, line, channel_id)
            VALUES (?, ?, ?, ?)
            """,
            (fixture_id, period, line, str(channel_id)),
        )
        await self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return cursor.lastrowid

    async def set_question_message(self, question_id: int, message_id: int) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE hilo_questions SET message_id = ? WHERE id = ?", (str(message_id), question_id)
        )
        await self._conn.commit()

    async def get_open_question(self, fixture_id: int, period: str) -> dict | None:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id, line, channel_id, message_id, resolved FROM hilo_questions
            WHERE fixture_id = ? AND period = ? AND resolved = 0
            """,
            (fixture_id, period),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row[0], "line": row[1], "channel_id": row[2], "message_id": row[3], "resolved": row[4]}

    async def list_open_questions(self) -> list[dict]:
        """Every unresolved, already-posted question, regardless of which
        process created it -- bot.py uses this on startup to re-register
        (Client.add_view()) buttons that were still open when it (or a
        --broadcast replay) last stopped, so clicking them keeps working.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT id, fixture_id, period, line, channel_id, message_id FROM hilo_questions
            WHERE resolved = 0 AND message_id IS NOT NULL
            """
        )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "fixture_id": r[1], "period": r[2], "line": r[3], "channel_id": r[4], "message_id": r[5]}
            for r in rows
        ]

    async def record_guess(self, question_id: int, external_id: str, guess: str) -> bool:
        """Returns True if this is the user's first guess on this question,
        False if they'd already guessed (guess is not overwritten).
        """
        assert self._conn is not None
        user_id = await self._get_or_create_user_id(external_id)
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO hilo_guesses (question_id, user_id, guess) VALUES (?, ?, ?)",
            (question_id, user_id, guess),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def resolve_question(self, question_id: int, outcome: str) -> list[tuple[str, str]]:
        """Marks the question resolved and returns every (external_id, guess)
        that was recorded against it, for the caller to score.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT users.external_id, hilo_guesses.guess FROM hilo_guesses
            JOIN users ON users.id = hilo_guesses.user_id
            WHERE hilo_guesses.question_id = ?
            """,
            (question_id,),
        )
        guesses = [(row[0], row[1]) for row in await cursor.fetchall()]
        await self._conn.execute(
            "UPDATE hilo_questions SET resolved = 1, outcome = ? WHERE id = ?", (outcome, question_id)
        )
        await self._conn.commit()
        return guesses

    async def bump_streak(self, external_id: str, correct: bool) -> tuple[int, int, bool]:
        """Applies one Hi-Lo result for `external_id`. Returns
        (new_count, new_best_count, is_new_server_record).
        """
        assert self._conn is not None
        user_id = await self._get_or_create_user_id(external_id)

        cursor = await self._conn.execute("SELECT MAX(best_count) FROM streaks WHERE streak_type = 'hilo'")
        row = await cursor.fetchone()
        prior_server_best = row[0] or 0

        cursor = await self._conn.execute(
            "SELECT count, best_count FROM streaks WHERE user_id = ? AND streak_type = 'hilo'", (user_id,)
        )
        row = await cursor.fetchone()
        count, best_count = row if row is not None else (0, 0)

        new_count = count + 1 if correct else 0
        new_best = max(best_count, new_count)

        await self._conn.execute(
            """
            INSERT INTO streaks (user_id, streak_type, count, best_count)
            VALUES (?, 'hilo', ?, ?)
            ON CONFLICT(user_id, streak_type)
            DO UPDATE SET count = excluded.count, best_count = excluded.best_count,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, new_count, new_best),
        )
        await self._conn.commit()

        is_new_record = correct and new_best > prior_server_best and new_best > 0
        return new_count, new_best, is_new_record

    async def leaderboard(self, limit: int = 10) -> list[tuple[str, int, int]]:
        """Top (external_id, current_count, best_count) by best streak."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            SELECT users.external_id, streaks.count, streaks.best_count FROM streaks
            JOIN users ON users.id = streaks.user_id
            WHERE streaks.streak_type = 'hilo'
            ORDER BY streaks.best_count DESC, streaks.count DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [(row[0], row[1], row[2]) for row in await cursor.fetchall()]
