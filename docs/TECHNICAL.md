# Technical writeup

This covers the parts worth explaining in more depth than the code comments:
how events are detected from a messy real-world feed, the deployment stack,
and a handful of real bugs found (and fixed) while building and testing
against the live TxLINE devnet API, Discord, and Postgres.

## Event detection

`app/detector.py` turns two raw feeds -- `ScoreUpdate` (from
`/scores/historical/{fixtureId}` or `/scores/stream`) and `OddsUpdate`
(from `/odds/stream`) -- into six clean `Event` types: `MATCH_START`,
`HALF_TIME`, `GOAL`, `RED_CARD`, `MATCH_END`, `ODDS_SWING`.

**The feed is an amendment log, not a clean event log.** The same logical
happening (a goal, a card) is typically posted 2-3 times under the same
`Id`, with `Confirmed` going `false -> true` as `Data` gets filled in, and
can be retracted entirely via a later record with
`Action == "action_discarded"` referencing that `Id` (the `Score` field
then reverts). This was verified against a real fixture (`18179549`) that
contained exactly one discarded phantom goal. The detector handles this by:

- Only trusting `Score` changes on records where `Confirmed is not False`
  (a proposed-then-discarded goal must never bump the count).
- Keying off the *monotonically tracked* goal/red-card totals per
  participant, not the `Action == "goal"` tag directly -- so a discarded
  proposal simply never raises the tracked total, and there's nothing to
  revert.

**Match state is derived from `Action`, not `GameState`.** `GameState` was
observed to be inconsistent across endpoints: an int on `/fixtures/snapshot`
(e.g. `3` = finished), `null` on `/odds/snapshot`, and a constant string
(`"scheduled"`) on every single record from `/scores/historical` regardless
of actual match progress. Instead, `Action` values were sampled directly
from a real fixture's full history and found reliable:

```
kickoff              -> MATCH_START (first occurrence only)
halftime_finalised   -> HALF_TIME
game_finalised       -> MATCH_END
```

**Idempotency** is enforced by a `processed_events` table (an insert-if-new
"was this exact `dedupe_key` seen before" check) that every `Event` passes
through before being queued. This is what makes replaying the same fixture
twice a no-op instead of a duplicate-event source, and what lets `bot.py`
safely re-fetch and re-subscribe to live streams after any restart without
re-announcing old news.

**The Hi-Lo game tracks its own goal count independently of the detector**,
mirroring how `pundit.py` keeps its own running score. This was necessary
because the feed's `H2` (2nd-half) goals subtotal was observed to stay
`null` even on records that logically should have populated it (a real
`ScoreBlock.Participant.H2.Goals` stayed `None` through an entire fixture
that had second-half goals in the *combined* total). `game.py` instead
computes 2nd-half goals as `final_total - goals_at_halftime`, both derived
from the same `GOAL` events the pundit already sees.

### Docs-vs-reality mismatches found during onboarding

- `/scores/historical/{fixtureId}` is documented as returning
  `application/json` with lowerCamelCase fields. In reality it returns an
  SSE-formatted body (`data: {...}\nid: N\n\n` blocks) with PascalCase
  fields -- the same shape as the documented `/scores/stream` events.
  `txline_client.py` parses it as SSE regardless of the declared
  content-type.
- `/api/odds/snapshot/{fixtureId}` matched its documented schema exactly.

## Deployment stack

One Docker container (`python:3.11-slim`) runs `python -m app.main serve`,
which starts three things in one asyncio event loop:

1. The Discord bot (`app/bot.py`) -- slash commands + the live
   ingestion/dispatch pipeline.
2. A FastAPI status page (`app/web.py`) via Uvicorn, bound to `$PORT`.
3. Whichever of the two stops first (crash, or a host-issued SIGTERM)
   triggers a coordinated shutdown of the other, via
   `asyncio.wait(..., return_when=FIRST_COMPLETED)` rather than a plain
   `gather()` (which would either leave the survivor running forever, or
   swallow the real exception behind a bare `CancelledError`).

**Hosting**: Render (free web service tier) + Neon (free Postgres),
picked specifically because Render's free tier has no persistent disk --
so `storage.py` supports both aiosqlite (local dev, zero setup) and
asyncpg/Postgres (`DATABASE_URL` set), sharing the same method surface.
See `docs/DEPLOY.md` for the full walkthrough.

**Render's free tier sleeps** after ~15 minutes with no incoming HTTP
requests (background websocket/SSE activity doesn't count as "activity"
for this). An UptimeRobot monitor pings `/health` every 5 minutes to keep
it warm. If a sleep/wake cycle does happen anyway, it's a full container
restart, not a pause/resume -- everything that needed to survive that
(Discord reconnect, Hi-Lo button persistence, TxLINE auth/stream
reconnect) is covered by the findings below, not by anything sleep-cycle
-specific.

## Engineering findings

A few real bugs, caught by testing against live systems rather than
assumed away:

### Postgres `INTEGER` overflow on epoch-millisecond timestamps

`Event.ts` (and the `processed_events.ts` column) is epoch-*milliseconds*
-- roughly 13 digits, e.g. `1783047619870`. SQLite's dynamically-typed
`INTEGER` stores this fine, but Postgres's `INTEGER` is a fixed 4-byte type
(max ~2.1 billion) and would silently overflow. Caught during storage.py's
Postgres port by explicitly checking the value's magnitude against
Postgres's actual limits before writing the schema, not just copying the
SQLite column type across -- the Postgres schema uses `BIGINT` for `ts`.
Verified live: wrote and read back `ts=123456789012` against a real Neon
database with no truncation.

### `/token/activate` is one-time-use per on-chain `txSig`

The original hardening plan (Phase 5) was: on a TxLINE 401, re-run the
*full* guest-JWT + wallet-signature + token-activation flow (porting
`onboarding/subscribe.ts`'s logic to Python). Live-testing that port
(`app/wallet_auth.py`) against the real devnet API returned:

```
403 Forbidden: "This transaction has already been used to activate a subscription"
```

i.e. activation is a one-time action tied to the on-chain `txSig`, not a
repeatable refresh mechanism -- confirmed by re-running the exact same,
correctly-signed request that had already succeeded once. Two
consequences, both reflected in the shipped code:

1. `app/txline_auth.py`'s on-401 refresh only re-requests a guest JWT
   (`POST /auth/guest/start`, no signature needed) with backoff + logging
   -- it does **not** call `/token/activate`. The API token is long-lived
   for the whole paid subscription; only the JWT needs periodic
   refreshing.
2. A deployed instance with no local `credentials.json` (e.g. Render, no
   persistent disk) can't bootstrap fresh credentials via a new activation
   either, since it would reuse the same already-spent `txSig`. Instead,
   `config.py` accepts `TXLINE_CREDENTIALS_JSON` -- the *existing*,
   already-activated `credentials.json` content, carried forward as one
   env var.

`wallet_auth.py` itself was still verified correct (it derives the exact
same wallet public key `onboarding/subscribe.ts`'s on-chain subscribe
recorded) and is kept as tested reference code for the one time it *would*
be needed again: activating a subscription from a genuinely new on-chain
`subscribe()` transaction.

### Replay pacing: a stray days-early metadata record

Historical fixture data was found to include a coverage-scheduling
artifact -- a `coverage_update`/`comment` record posted up to ~4.6 days
before the real pre-match buildup starts. `replay.py`'s pacing loop
(sleeping proportionally to the gap between consecutive records, divided
by `--speed`) would turn that single gap into hours of real wall-clock
sleep regardless of speed multiplier. Fixed two ways:

1. Any single inter-record gap is capped at 30 minutes before being paced
   (the largest *legitimate* in-match gap sampled across several real
   fixtures was ~14 minutes, so this only ever clips the artifact).
2. Pre-kickoff records are fed through with no pacing delay at all (they
   carry no `Event`s anyway), and real-time pacing only starts from the
   `kickoff` record onward -- so `--speed` behaves consistently regardless
   of how far in advance a given fixture's metadata was posted.

### Discord button views don't survive their creating process dying

A Hi-Lo question's guess buttons are tied, in `discord.py`, to the
specific client connection that sent the message. If that process exits
(a `replay.py --broadcast` run finishing, or `bot.py` restarting) while
the question is still open, clicking the old message's buttons fails with
"This interaction failed" -- reproduced live during testing. Fixed by
building the views as *persistent* (`timeout=None`, a stable `custom_id`
per button -- both already required by discord.py for this) and having
`bot.py` re-register (`Client.add_view()`) every still-unresolved question
found in storage on startup, regardless of which process originally
posted it, since storage is shared. This also means a question posted by
a short-lived `--broadcast` replay keeps working after that process exits,
as long as `bot.py` is running.

### A single long-lived Postgres connection vs. Neon's free-tier suspend/pooler

The deployed status page started returning 500 partway through a live
demo session, having worked fine right after deploy. `storage.py`'s
Postgres backend held one `asyncpg` connection open for the entire
process lifetime. Reproduced locally against the exact same live Neon
data first: the render pipeline itself (same query results, same
`_render_page()` call) worked with a *fresh* connection, which pointed
away from a code bug in the rendering path and toward the long-lived
connection specifically. Two real, independent hazards line up with a
single persistent connection to Neon's free tier:

1. Neon's free-tier compute suspends after a period of inactivity; a
   connection open across that suspend can go stale, failing every
   subsequent query for the rest of the process's life rather than just
   the one request.
2. The connection string here uses Neon's `-pooler` (PgBouncer,
   transaction-mode) endpoint. asyncpg caches prepared statements per
   connection by default, and that cache doesn't survive PgBouncer routing
   the same logical connection to a different backend Postgres connection
   -- a well-documented asyncpg+PgBouncer incompatibility.

Fixed by switching to `asyncpg.create_pool()` (so a connection that goes
stale is quietly replaced rather than reused forever) with
`statement_cache_size=0` (the standard mitigation for #2). Verified against
the real Neon database -- pool creation, the full CRUD surface, and the
exact status-page render call that had been 500ing -- then confirmed live
on the redeployed instance (`/` and `/health` both `200`, leaderboard
rendering correctly).

### Windows console encoding

Nepali/Hindi commentary is Devanagari script; Windows terminals default to
`cp1252`, which raises `UnicodeEncodeError` on non-Latin output.
`replay.py`'s CLI entry point reconfigures `stdout`/`stderr` to UTF-8 at
startup when they aren't already, rather than requiring
`PYTHONIOENCODING=utf-8` to be set manually every run.
