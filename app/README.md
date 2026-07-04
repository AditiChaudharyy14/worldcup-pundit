# TxLINE ingestion core (Phase 2)

Python ingestion layer for the TxODDS World Cup Hackathon (Consumer & Fan
Experiences track). Consumes TxLINE devnet fixtures/scores/odds and turns raw
updates into fan-facing `Event`s (GOAL, RED_CARD, MATCH_START, MATCH_END,
ODDS_SWING). No web framework yet -- that's a later phase.

## Setup

Requires `../onboarding/credentials.json` to already exist (see
`../onboarding/subscribe.ts`).

```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # .venv/bin/pip on macOS/Linux
```

## Usage

```
python -m app.main live
python -m app.main replay --fixture 18179549 --speed 10
```

`replay.py` is also runnable standalone: `python -m app.replay --fixture X --speed 10`.

## Layout

| File | Responsibility |
|---|---|
| `config.py` | Loads `credentials.json` + tunables (pydantic-settings). JWT/API token are `SecretStr` -- never printable by accident. |
| `models.py` | Pydantic models built from *observed* API responses, not the docs. See its docstring for docs-vs-reality mismatches found during onboarding. |
| `txline_auth.py` | `httpx.Auth` that attaches `Authorization`/`X-Api-Token` and transparently refreshes the guest JWT + retries once on 401. |
| `txline_client.py` | Async client: fixtures snapshot, historical scores (SSE-formatted despite docs), live SSE streams with reconnect/backoff. |
| `detector.py` | Stateful per-fixture logic turning raw updates into `Event`s. Only trusts score changes when `Confirmed is not False`, since the feed proposes-then-sometimes-discards goals/cards. |
| `storage.py` | aiosqlite. `processed_events` is the idempotency ledger; `users`/`subscriptions`/`streaks` are scaffolded empty for a later phase. |
| `replay.py` | Fetches historical scores for a fixture and re-feeds them through the same `Detector` at a speed multiplier. |
| `main.py` | CLI: `live` (today's fixtures -> streams -> detector) and `replay` (delegates to `replay.py`). |

## Notable real-world quirks (see models.py for detail)

- `/scores/historical/{fixtureId}` is documented as JSON but actually returns
  SSE-formatted text with PascalCase fields -- `txline_client.py` parses it
  as SSE.
- The `GameState` field is inconsistent/unreliable across endpoints; match
  start/end are derived from `Action == "kickoff"` / `"game_finalised"`
  instead.
- Goals/cards are proposed (`Confirmed: false`) then confirmed or discarded;
  verified against a real fixture with a discarded phantom goal.

## Tests

```
python -m pytest app/tests -v
```

The end-to-end test hits the real devnet API using `credentials.json` and is
skipped automatically if that file is absent.
