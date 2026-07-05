# TxLINE ingestion core + AI pundit (Phase 3)

Python ingestion layer for the TxODDS World Cup Hackathon (Consumer & Fan
Experiences track). Consumes TxLINE devnet fixtures/scores/odds, turns raw
updates into fan-facing `Event`s (GOAL, RED_CARD, MATCH_START, MATCH_END,
ODDS_SWING), and turns those into short pundit commentary via the Anthropic
API. No web framework yet -- that's a later phase.

## Setup

Requires `../onboarding/credentials.json` to already exist (see
`../onboarding/subscribe.ts`), and an `ANTHROPIC_API_KEY` in a repo-root
`.env` (copy `.env.example`) if you want LLM commentary (`--no-llm` skips it).

```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # .venv/bin/pip on macOS/Linux
```

## Usage

```
python -m app.main live --lang en
python -m app.main replay --fixture 18179549 --speed 10 --lang ne
python -m app.main replay --fixture 18179549 --speed 10 --no-llm   # templates only, no API cost
```

`replay.py` is also runnable standalone: `python -m app.replay --fixture X --speed 10 --lang hi`.

## Layout

| File | Responsibility |
|---|---|
| `config.py` | Loads `credentials.json` + tunables (pydantic-settings). JWT/API token/`ANTHROPIC_API_KEY` are `SecretStr` -- never printable by accident. |
| `models.py` | Pydantic models built from *observed* API responses, not the docs. See its docstring for docs-vs-reality mismatches found during onboarding. |
| `txline_auth.py` | `httpx.Auth` that attaches `Authorization`/`X-Api-Token` and transparently refreshes the guest JWT + retries once on 401. |
| `txline_client.py` | Async client: fixtures snapshot, historical scores (SSE-formatted despite docs), live SSE streams with reconnect/backoff. |
| `detector.py` | Stateful per-fixture logic turning raw updates into `Event`s. Only trusts score changes when `Confirmed is not False`, since the feed proposes-then-sometimes-discards goals/cards. |
| `storage.py` | aiosqlite. `processed_events` is the idempotency ledger; `users`/`subscriptions`/`streaks` are scaffolded empty for a later phase. |
| `prompts.py` | Pundit persona (system prompt) + 2-3 few-shot examples per language (`en`/`ne`/`hi`), sent as ordinary earlier messages (Sonnet 4.6 rejects assistant-turn prefill). |
| `pundit.py` | Consumes `Event`s, batches everything for a fixture within a 60s *match-time* window into one Anthropic call (`claude-sonnet-4-6`, `thinking: disabled`, `effort: low`, ~150 tokens), with template fallback on any API error. |
| `replay.py` | Fetches historical scores for a fixture, looks up team names, and re-feeds updates through the same `Detector` + `Pundit` at a speed multiplier. |
| `main.py` | CLI: `live` (today's fixtures -> streams -> detector -> pundit) and `replay` (delegates to `replay.py`). |

## Notable real-world quirks (see models.py for detail)

- `/scores/historical/{fixtureId}` is documented as JSON but actually returns
  SSE-formatted text with PascalCase fields -- `txline_client.py` parses it
  as SSE.
- The `GameState` field is inconsistent/unreliable across endpoints; match
  start/end are derived from `Action == "kickoff"` / `"game_finalised"`
  instead.
- Goals/cards are proposed (`Confirmed: false`) then confirmed or discarded;
  verified against a real fixture with a discarded phantom goal.

## Pundit behavior notes

- Batching is keyed off each `Event`'s own `ts` (match time), not wall-clock
  time, so a `--speed 1000` replay batches identically to live play instead
  of a 60-real-second window swallowing the entire sped-up match.
- Commentary never gives gambling advice; `ODDS_SWING` is explained as a
  factual market/probability move only (see `prompts.py`).
- On any Anthropic API failure the pipeline falls back to a plain template
  message and logs the error -- it never stalls.

## Tests

```
python -m pytest app/tests -v
```

The end-to-end test hits the real devnet API using `credentials.json` and is
skipped automatically if that file is absent.
