# TxLINE ingestion core + AI pundit + Discord bot (Phase 5: deployed)

Python ingestion layer for the TxODDS World Cup Hackathon (Consumer & Fan
Experiences track). Consumes TxLINE devnet fixtures/scores/odds, turns raw
updates into fan-facing `Event`s (GOAL, RED_CARD, MATCH_START, HALF_TIME,
MATCH_END, ODDS_SWING), turns those into short pundit commentary via the
Anthropic API, serves both that commentary and a Hi-Lo streak game to a
Discord server (Telegram was the original plan but is blocked in Nepal,
hence Discord), and (Phase 5) runs as one deployable unit -- bot + ingestion
+ a small FastAPI status page -- with a hardened JWT refresh and a
Postgres-backed storage option for hosts with no persistent disk. See
`../docs/DEPLOY.md` for the actual Render + Neon deployment walkthrough.

## Setup

Requires `../onboarding/credentials.json` to already exist (see
`../onboarding/subscribe.ts`), and a repo-root `.env` (copy `.env.example`):
`ANTHROPIC_API_KEY` for LLM commentary (`--no-llm` skips it), and
`DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` (+ optional `DISCORD_GUILD_ID`) if
you're running the bot or `replay --broadcast`. `DATABASE_URL`/`DATA_DIR`/
`TXLINE_CREDENTIALS_JSON` are deployment-only -- see `../docs/DEPLOY.md`.

```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # .venv/bin/pip on macOS/Linux
```

## Usage

```
python -m app.main live --lang en
python -m app.main replay --fixture 18179549 --speed 10 --lang ne
python -m app.main replay --fixture 18179549 --speed 10 --no-llm   # templates only, no API cost
python -m app.main replay --fixture 18179549 --speed 60 --broadcast  # push into the real Discord bot

python -m app.bot   # start the Discord bot: slash commands + live pundit/Hi-Lo posting
python -m app.main serve  # bot + ingestion + FastAPI status page together, one process (deployment)
```

`replay.py` is also runnable standalone: `python -m app.replay --fixture X --speed 10 --lang hi`.

### Discord bot

Once `python -m app.bot` is running and invited to your server:

- `/start` or `/lang` -- pick your commentary language (en/ne/hi) via buttons.
- `/matches` -- browse today's fixtures, follow/unfollow via buttons (ephemeral, per-user).
- `/leaderboard` -- top Hi-Lo streak players.

Pundit commentary and the Hi-Lo game both post to the single channel set via
`DISCORD_CHANNEL_ID` (one channel for every fixture -- see `dispatcher.py`'s
docstring for why, given this is a single-test-server hackathon build). For
a fixture nobody's followed yet, commentary defaults to showing all three
languages rather than staying silent (`config.default_broadcast_langs`).

`replay --broadcast` reuses the exact same `DiscordDispatcher`/`HiLoGame`
classes the live bot uses, via a lightweight `discord.Client` login, so it's
a faithful dry run for rehearsing/recording the demo.

## Layout

| File | Responsibility |
|---|---|
| `config.py` | Loads `credentials.json` (or `TXLINE_CREDENTIALS_JSON`, for hosts with no persistent disk) + tunables (pydantic-settings). JWT/API token/`ANTHROPIC_API_KEY` are `SecretStr` -- never printable by accident. |
| `models.py` | Pydantic models built from *observed* API responses, not the docs. See its docstring for docs-vs-reality mismatches found during onboarding. |
| `wallet_auth.py` | Ports `onboarding/subscribe.ts`'s guest-JWT + wallet-signature + token-activation flow into Python (PyNaCl Ed25519). Verified correct (derives the same wallet public key onboarding recorded) but **not** on the auto-refresh path -- `/token/activate` is one-time-use per on-chain txSig, confirmed live. Kept as a reference for activating a *new* subscription later. |
| `txline_auth.py` | `httpx.Auth` that attaches `Authorization`/`X-Api-Token` and transparently refreshes the guest JWT (with backoff + logging) + retries once on 401. Only the JWT is refreshed -- see `wallet_auth.py` for why the API token isn't. |
| `txline_client.py` | Async client: fixtures snapshot, historical scores (SSE-formatted despite docs), live SSE streams with reconnect/backoff. |
| `detector.py` | Stateful per-fixture logic turning raw updates into `Event`s. Only trusts score changes when `Confirmed is not False`, since the feed proposes-then-sometimes-discards goals/cards. |
| `storage.py` | aiosqlite (local dev) or asyncpg/Postgres (`DATABASE_URL` set, e.g. a free Neon project -- see `../docs/DEPLOY.md`). `processed_events` is the idempotency ledger; `users` (external_id + lang), `subscriptions` (fixture follows), `streaks`, and `hilo_questions`/`hilo_guesses` back the Discord bot. |
| `state.py` | Tiny in-process registry (bot readiness, start time) so `web.py` can read live state -- everything runs in one process under `serve`, no IPC needed. |
| `web.py` | FastAPI status page (`GET /`, mobile-friendly, no login) and `GET /health` JSON -- judges' entry point: bot online/uptime, events processed, last 5 events, Hi-Lo leaderboard, Discord invite + GitHub links. |
| `prompts.py` | Pundit persona (system prompt) + 2-3 few-shot examples per language (`en`/`ne`/`hi`), sent as ordinary earlier messages (Sonnet 4.6 rejects assistant-turn prefill). Also `LANGUAGE_LABELS` (flag + native name) for Discord buttons/embeds. |
| `pundit.py` | Consumes `Event`s, batches everything for a fixture within a 60s *match-time* window into one Anthropic call (`claude-sonnet-4-6`, `thinking: disabled`, `effort: low`, ~150 tokens), with template fallback on any API error. |
| `dispatcher.py` | `DiscordDispatcher`: runs one `Pundit` per `(fixture_id, lang)` actively followed, each posting a rich embed to the designated channel instead of printing -- "one Claude call per event-batch per language" falls out of reusing `Pundit`'s own batching unmodified. |
| `game.py` | `HiLoGame`: posts a "more/fewer than N combined goals this half?" button question at `MATCH_START`/`HALF_TIME`, resolves it at the next half/full-time boundary, updates streaks, announces new server records. |
| `handlers.py` | Slash commands (`/start`, `/lang`, `/matches`, `/leaderboard`) registered onto the bot's `CommandTree`. |
| `bot.py` | `python -m app.bot` -- the Discord bot process: registers slash commands, then runs the same live-streaming pipeline as `main.py live` but dispatches to Discord instead of stdout. |
| `replay.py` | Fetches historical scores for a fixture, looks up team names, and re-feeds updates through the same `Detector` at a speed multiplier -- to a single-language `Pundit` printing to stdout, or (`--broadcast`) to `DiscordDispatcher` + `HiLoGame` via a lightweight logged-in `discord.Client`. |
| `main.py` | CLI: `live`, `replay` (delegates to `replay.py`), and `serve` (Phase 5 -- bot + ingestion + `web.py` together in one process, with coordinated shutdown if either half stops). |

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

## Hi-Lo game notes

- `HALF_TIME` (`Action == "halftime_finalised"`) resolves the 1st-half
  question and opens the 2nd-half one; `MATCH_END` resolves the 2nd half as
  `final total - goals at half`, since the feed's `H2` goals subtotal was
  observed to stay `null` even on records that should have populated it --
  `game.py` tracks combined goals itself off the `GOAL` event stream instead.
- A failed Discord send (question post, resolution post, or a guess button
  callback) is caught and logged, never raised -- one bad send can't crash
  the ingestion pipeline or the bot.

## Deployment

`../Dockerfile` builds a `python:3.11-slim` image running `python -m app.main
serve`; `../.dockerignore` keeps `.env`/`credentials.json`/`node_modules`/
`.venv` out of it. See `../docs/DEPLOY.md` for the full Render + Neon
click-by-click walkthrough, including every env var the host needs and an
UptimeRobot keepalive setup (Render's free tier sleeps after ~15 min with no
incoming HTTP requests -- background Discord/TxLINE activity doesn't count).

## Tests

```
python -m pytest app/tests -v
```

The end-to-end test hits the real devnet API using `credentials.json` and is
skipped automatically if that file is absent.
