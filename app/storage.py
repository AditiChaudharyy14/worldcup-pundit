"""Persistence, backed by either SQLite (aiosqlite, local dev) or Postgres
(asyncpg, e.g. a free Neon project -- see docs/DEPLOY.md) depending on
whether DATABASE_URL is set (see config.py). Render's free web-service tier
has no persistent disk, so the SQLite file wouldn't survive a redeploy or a
free-tier sleep/wake cycle there; Postgres is the durable option for that
host, while local dev keeps the zero-setup SQLite file.

Every public method's SQL is written once with '?' placeholders (SQLite's
native style) and translated to Postgres's '$1, $2, ...' via _ph() --the two
backends' bindable-parameter syntax is the only routine difference between
them for the queries this file needs, since Postgres also supports SQLite's
"INSERT ... ON CONFLICT ... DO UPDATE SET x = excluded.x" upsert syntax
verbatim. The one structural difference is SQLite's "INSERT OR IGNORE",
which Postgres has no keyword for -- those call sites go through
_insert_ignore()/_insert_ignore_returning_id() instead, which builds the
"ON CONFLICT (...) DO NOTHING [RETURNING ...]" form for Postgres.

processed_events is the idempotency ledger detector.py checks before ever
putting an Event on its queue: an insert-if-new + "was it new" bool tells us
in one round trip whether this exact event has already been emitted (by a
previous run, or an earlier point in this same run).

users / subscriptions / streaks / hilo_questions / hilo_guesses back the
Discord bot's language prefs, fixture-follow list, and Hi-Lo streak game
state (see bot.py/handlers.py/game.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from app.models import Event

_SCHEMA_SQLITE = """
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
    display_name TEXT,
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

# ts is epoch-milliseconds (~13 digits) -- BIGINT, not Postgres's 4-byte
# INTEGER (max ~2.1e9), which would overflow. Everything else fits INTEGER.
_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS processed_events (
    dedupe_key TEXT PRIMARY KEY,
    fixture_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    ts BIGINT NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    external_id TEXT UNIQUE NOT NULL,
    lang TEXT NOT NULL DEFAULT 'en',
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    fixture_id INTEGER NOT NULL,
    event_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, fixture_id)
);

CREATE TABLE IF NOT EXISTS streaks (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    streak_type TEXT NOT NULL DEFAULT 'hilo',
    count INTEGER NOT NULL DEFAULT 0,
    best_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, streak_type)
);

CREATE TABLE IF NOT EXISTS hilo_questions (
    id SERIAL PRIMARY KEY,
    fixture_id INTEGER NOT NULL,
    period TEXT NOT NULL,
    line REAL NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(fixture_id, period)
);

CREATE TABLE IF NOT EXISTS hilo_guesses (
    id SERIAL PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES hilo_questions(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    guess TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(question_id, user_id)
);
"""


class Storage:
    def __init__(self, sqlite_path: Path | str, database_url: str | None = None):
        self._sqlite_path = str(sqlite_path)
        self._database_url = database_url
        self._is_postgres = bool(database_url)
        self._conn: Any = None  # aiosqlite.Connection (SQLite only)
        self._pool: Any = None  # asyncpg.Pool (Postgres only)

    async def connect(self) -> "Storage":
        if self._is_postgres:
            import asyncpg  # imported lazily -- local dev never needs it installed to matter

            # A pool (not a single long-lived connection) so a connection that
            # goes stale -- e.g. Neon's free tier suspends its compute after
            # inactivity -- gets quietly replaced instead of failing every
            # query for the rest of the process's life. statement_cache_size=0
            # because Neon's connection string here is the "-pooler" (PgBouncer,
            # transaction-mode) endpoint: asyncpg's prepared-statement cache
            # doesn't survive being routed to a different backend connection
            # under that, and is the standard asyncpg+PgBouncer mitigation.
            self._pool = await asyncpg.create_pool(
                self._database_url, min_size=1, max_size=5, statement_cache_size=0
            )
            await self._pool.execute(_SCHEMA_POSTGRES)
            # Postgres supports ADD COLUMN IF NOT EXISTS natively -- safe to
            # run unconditionally every connect(), unlike SQLite below.
            await self._pool.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT")
        else:
            self._conn = await aiosqlite.connect(self._sqlite_path)
            await self._migrate_legacy_scaffold()
            await self._conn.executescript(_SCHEMA_SQLITE)
            await self._conn.commit()
            await self._ensure_sqlite_display_name_column()
        return self

    async def _ensure_sqlite_display_name_column(self) -> None:
        """display_name was added after users tables already existed locally
        (unlike the phase-2 scaffold, this one has real data -- never drop
        it). SQLite has no ADD COLUMN IF NOT EXISTS, so check first.
        """
        assert self._conn is not None
        cursor = await self._conn.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "display_name" not in columns:
            await self._conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
            await self._conn.commit()

    async def _migrate_legacy_scaffold(self) -> None:
        """The phase-2 users/subscriptions/streaks tables predate the columns
        and constraints the bot needs. They were never written to (no code
        path did so before phase 4), so it's safe to drop and let the schema
        recreate them rather than hand-write an ALTER TABLE path. SQLite-only:
        a fresh Postgres database never has this legacy scaffold.
        """
        assert self._conn is not None
        cursor = await self._conn.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in await cursor.fetchall()}
        if columns and "lang" not in columns:
            for table in ("hilo_guesses", "hilo_questions", "streaks", "subscriptions", "users"):
                await self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            await self._conn.commit()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Storage":
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- backend-dispatching helpers ----------------------------------------

    def _ph(self, sql: str) -> str:
        """Translates '?' positional placeholders to Postgres's '$1, $2, ...'
        style; a no-op for SQLite. Safe as a blind translation because every
        '?' in these query strings is a placeholder we wrote ourselves --
        parameter *values* are always bound, never interpolated into the SQL.
        """
        if not self._is_postgres:
            return sql
        parts = []
        n = 0
        for ch in sql:
            if ch == "?":
                n += 1
                parts.append(f"${n}")
            else:
                parts.append(ch)
        return "".join(parts)

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        if self._is_postgres:
            await self._pool.execute(self._ph(sql), *params)
        else:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def _fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        if self._is_postgres:
            row = await self._pool.fetchrow(self._ph(sql), *params)
            return tuple(row) if row is not None else None
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._is_postgres:
            rows = await self._pool.fetch(self._ph(sql), *params)
            return [tuple(r) for r in rows]
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchall()

    async def _insert_ignore(
        self, table: str, columns: list[str], values: tuple, conflict_cols: list[str]
    ) -> bool:
        """INSERT ... ON CONFLICT DO NOTHING (or SQLite's INSERT OR IGNORE),
        unified across backends. Returns True iff a new row was inserted.
        """
        col_list = ", ".join(columns)
        if self._is_postgres:
            placeholders = ", ".join(f"${i + 1}" for i in range(len(values)))
            conflict = ", ".join(conflict_cols)
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO NOTHING RETURNING 1"
            row = await self._pool.fetchrow(sql, *values)
            return row is not None
        placeholders = ", ".join("?" for _ in values)
        sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
        cursor = await self._conn.execute(sql, values)
        await self._conn.commit()
        return cursor.rowcount == 1

    async def _insert_ignore_returning_id(
        self, table: str, columns: list[str], values: tuple, conflict_cols: list[str]
    ) -> int | None:
        """Same as _insert_ignore(), but returns the new row's id (None if
        the insert was skipped due to conflict).
        """
        col_list = ", ".join(columns)
        if self._is_postgres:
            placeholders = ", ".join(f"${i + 1}" for i in range(len(values)))
            conflict = ", ".join(conflict_cols)
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO NOTHING RETURNING id"
            row = await self._pool.fetchrow(sql, *values)
            return row[0] if row is not None else None
        placeholders = ", ".join("?" for _ in values)
        sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
        cursor = await self._conn.execute(sql, values)
        await self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return cursor.lastrowid

    # -- idempotency ledger --------------------------------------------------

    async def try_mark_processed(self, event: Event) -> bool:
        """Atomically records `event` as processed. Returns True the first
        time a given dedupe_key is seen (caller should emit), False on any
        repeat (caller should skip) -- this is what makes replaying the same
        fixture twice a no-op rather than a duplicate-event source.
        """
        return await self._insert_ignore(
            "processed_events",
            ["dedupe_key", "fixture_id", "event_type", "ts", "payload"],
            (event.dedupe_key, event.fixture_id, event.type, event.ts, json.dumps(event.payload)),
            conflict_cols=["dedupe_key"],
        )

    async def count_processed_events(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) FROM processed_events")
        return row[0] if row is not None else 0

    async def list_recent_events(self, limit: int = 5) -> list[dict]:
        """Most-recently-recorded events (by insertion time, not match time)
        -- for the status page (web.py), which wants "what just happened"
        regardless of any historical replay's own in-match timestamps.
        """
        rows = await self._fetchall(
            """
            SELECT fixture_id, event_type, ts, created_at FROM processed_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {"fixture_id": r[0], "event_type": r[1], "ts": r[2], "created_at": str(r[3])}
            for r in rows
        ]

    # -- users / language prefs -------------------------------------------

    async def _get_or_create_user_id(self, external_id: str) -> int:
        await self._insert_ignore("users", ["external_id"], (external_id,), conflict_cols=["external_id"])
        row = await self._fetchone("SELECT id FROM users WHERE external_id = ?", (external_id,))
        assert row is not None
        return row[0]

    async def get_user_lang(self, external_id: str) -> str:
        await self._get_or_create_user_id(external_id)
        row = await self._fetchone("SELECT lang FROM users WHERE external_id = ?", (external_id,))
        assert row is not None
        return row[0]

    async def set_user_lang(self, external_id: str, lang: str) -> None:
        await self._get_or_create_user_id(external_id)
        await self._execute("UPDATE users SET lang = ? WHERE external_id = ?", (lang, external_id))

    async def set_user_display_name(self, external_id: str, display_name: str) -> None:
        """Recorded opportunistically whenever we have interaction.user
        handy (currently: Hi-Lo guesses) so the web status page's
        leaderboard can show a name instead of a raw Discord ID -- Discord
        mentions (<@id>) already resolve names client-side, so this is only
        needed off-Discord.
        """
        await self._get_or_create_user_id(external_id)
        await self._execute("UPDATE users SET display_name = ? WHERE external_id = ?", (display_name, external_id))

    # -- fixture follow/unfollow --------------------------------------------

    async def toggle_subscription(self, external_id: str, fixture_id: int) -> bool:
        """Follows `fixture_id` if not already followed, else unfollows.
        Returns True if the user is now following, False if now unfollowed.
        """
        user_id = await self._get_or_create_user_id(external_id)
        row = await self._fetchone(
            "SELECT id FROM subscriptions WHERE user_id = ? AND fixture_id = ?", (user_id, fixture_id)
        )
        if row is None:
            await self._execute(
                "INSERT INTO subscriptions (user_id, fixture_id) VALUES (?, ?)", (user_id, fixture_id)
            )
            return True
        await self._execute("DELETE FROM subscriptions WHERE id = ?", (row[0],))
        return False

    async def is_subscribed(self, external_id: str, fixture_id: int) -> bool:
        row = await self._fetchone(
            """
            SELECT 1 FROM subscriptions
            JOIN users ON users.id = subscriptions.user_id
            WHERE users.external_id = ? AND subscriptions.fixture_id = ?
            """,
            (external_id, fixture_id),
        )
        return row is not None

    async def get_active_langs(self, fixture_id: int) -> set[str]:
        """Distinct languages of users currently following `fixture_id`."""
        rows = await self._fetchall(
            """
            SELECT DISTINCT users.lang FROM subscriptions
            JOIN users ON users.id = subscriptions.user_id
            WHERE subscriptions.fixture_id = ?
            """,
            (fixture_id,),
        )
        return {row[0] for row in rows}

    # -- Hi-Lo streak game ---------------------------------------------------

    async def create_question(
        self, fixture_id: int, period: str, line: float, channel_id: int
    ) -> int | None:
        """Creates the (fixture_id, period) question. Returns its id, or None
        if one already exists (idempotent against replayed/duplicate events).
        """
        return await self._insert_ignore_returning_id(
            "hilo_questions",
            ["fixture_id", "period", "line", "channel_id"],
            (fixture_id, period, line, str(channel_id)),
            conflict_cols=["fixture_id", "period"],
        )

    async def set_question_message(self, question_id: int, message_id: int) -> None:
        await self._execute(
            "UPDATE hilo_questions SET message_id = ? WHERE id = ?", (str(message_id), question_id)
        )

    async def get_open_question(self, fixture_id: int, period: str) -> dict | None:
        row = await self._fetchone(
            """
            SELECT id, line, channel_id, message_id, resolved FROM hilo_questions
            WHERE fixture_id = ? AND period = ? AND resolved = 0
            """,
            (fixture_id, period),
        )
        if row is None:
            return None
        return {"id": row[0], "line": row[1], "channel_id": row[2], "message_id": row[3], "resolved": row[4]}

    async def list_open_questions(self) -> list[dict]:
        """Every unresolved, already-posted question, regardless of which
        process created it -- bot.py uses this on startup to re-register
        (Client.add_view()) buttons that were still open when it (or a
        --broadcast replay) last stopped, so clicking them keeps working.
        """
        rows = await self._fetchall(
            """
            SELECT id, fixture_id, period, line, channel_id, message_id FROM hilo_questions
            WHERE resolved = 0 AND message_id IS NOT NULL
            """
        )
        return [
            {"id": r[0], "fixture_id": r[1], "period": r[2], "line": r[3], "channel_id": r[4], "message_id": r[5]}
            for r in rows
        ]

    async def record_guess(self, question_id: int, external_id: str, guess: str) -> bool:
        """Returns True if this is the user's first guess on this question,
        False if they'd already guessed (guess is not overwritten).
        """
        user_id = await self._get_or_create_user_id(external_id)
        return await self._insert_ignore(
            "hilo_guesses",
            ["question_id", "user_id", "guess"],
            (question_id, user_id, guess),
            conflict_cols=["question_id", "user_id"],
        )

    async def resolve_question(self, question_id: int, outcome: str) -> list[tuple[str, str]]:
        """Marks the question resolved and returns every (external_id, guess)
        that was recorded against it, for the caller to score.
        """
        rows = await self._fetchall(
            """
            SELECT users.external_id, hilo_guesses.guess FROM hilo_guesses
            JOIN users ON users.id = hilo_guesses.user_id
            WHERE hilo_guesses.question_id = ?
            """,
            (question_id,),
        )
        guesses = [(row[0], row[1]) for row in rows]
        await self._execute(
            "UPDATE hilo_questions SET resolved = 1, outcome = ? WHERE id = ?", (outcome, question_id)
        )
        return guesses

    async def bump_streak(self, external_id: str, correct: bool) -> tuple[int, int, bool]:
        """Applies one Hi-Lo result for `external_id`. Returns
        (new_count, new_best_count, is_new_server_record).
        """
        user_id = await self._get_or_create_user_id(external_id)

        row = await self._fetchone("SELECT MAX(best_count) FROM streaks WHERE streak_type = 'hilo'")
        prior_server_best = row[0] or 0 if row is not None else 0

        row = await self._fetchone(
            "SELECT count, best_count FROM streaks WHERE user_id = ? AND streak_type = 'hilo'", (user_id,)
        )
        count, best_count = row if row is not None else (0, 0)

        new_count = count + 1 if correct else 0
        new_best = max(best_count, new_count)

        await self._execute(
            """
            INSERT INTO streaks (user_id, streak_type, count, best_count)
            VALUES (?, 'hilo', ?, ?)
            ON CONFLICT(user_id, streak_type)
            DO UPDATE SET count = excluded.count, best_count = excluded.best_count,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, new_count, new_best),
        )

        is_new_record = correct and new_best > prior_server_best and new_best > 0
        return new_count, new_best, is_new_record

    async def leaderboard(self, limit: int = 10) -> list[tuple[str, str | None, int, int]]:
        """Top (external_id, display_name, current_count, best_count) by best
        streak. external_id stays first/unchanged for callers building a
        Discord mention (<@external_id> already resolves a name client-side);
        display_name is for callers with nowhere else to get a name from
        (the web status page).
        """
        rows = await self._fetchall(
            """
            SELECT users.external_id, users.display_name, streaks.count, streaks.best_count FROM streaks
            JOIN users ON users.id = streaks.user_id
            WHERE streaks.streak_type = 'hilo'
            ORDER BY streaks.best_count DESC, streaks.count DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [(row[0], row[1], row[2], row[3]) for row in rows]
