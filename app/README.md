# TxLINE ingestion core + AI pundit + Discord bot (Phase 4)

Python ingestion layer for the TxODDS World Cup Hackathon (Consumer & Fan
Experiences track). Consumes TxLINE devnet fixtures/scores/odds, turns raw
updates into fan-facing `Event`s (GOAL, RED_CARD, MATCH_START, HALF_TIME,
MATCH_END, ODDS_SWING), turns those into short pundit commentary via the
Anthropic API, and (Phase 4) serves both that commentary and a Hi-Lo streak
game to a Discord server. Telegram was the original plan but is blocked in
Nepal, hence Discord.

## Setup

Requires `../onboarding/credentials.json` to already exist (see
`../onboarding/subscribe.ts`), and a repo-root `.env` (copy `.env.example`):
`ANTHROPIC_API_KEY` for LLM commentary (`--no-llm` skips it), and
`DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` (+ optional `DISCORD_GUILD_ID`) if
you're running the bot or `replay --broadcast`.

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
| `config.py` | Loads `credentials.json` + tunables (pydantic-settings). JWT/API token/`ANTHROPIC_API_KEY` are `SecretStr` -- never printable by accident. |
| `models.py` | Pydantic models built from *observed* API responses, not the docs. See its docstring for docs-vs-reality mismatches found during onboarding. |
| `txline_auth.py` | `httpx.Auth` that attaches `Authorization`/`X-Api-Token` and transparently refreshes the guest JWT + retries once on 401. |
| `txline_client.py` | Async client: fixtures snapshot, historical scores (SSE-formatted despite docs), live SSE streams with reconnect/backoff. |
| `detector.py` | Stateful per-fixture logic turning raw updates into `Event`s. Only trusts score changes when `Confirmed is not False`, since the feed proposes-then-sometimes-discards goals/cards. |
| `storage.py` | aiosqlite. `processed_events` is the idempotency ledger; `users` (external_id + lang), `subscriptions` (fixture follows), `streaks`, and `hilo_questions`/`hilo_guesses` back the Discord bot. |
| `prompts.py` | Pundit persona (system prompt) + 2-3 few-shot examples per language (`en`/`ne`/`hi`), sent as ordinary earlier messages (Sonnet 4.6 rejects assistant-turn prefill). Also `LANGUAGE_LABELS` (flag + native name) for Discord buttons/embeds. |
| `pundit.py` | Consumes `Event`s, batches everything for a fixture within a 60s *match-time* window into one Anthropic call (`claude-sonnet-4-6`, `thinking: disabled`, `effort: low`, ~150 tokens), with template fallback on any API error. |
| `dispatcher.py` | `DiscordDispatcher`: runs one `Pundit` per `(fixture_id, lang)` actively followed, each posting a rich embed to the designated channel instead of printing -- "one Claude call per event-batch per language" falls out of reusing `Pundit`'s own batching unmodified. |
| `game.py` | `HiLoGame`: posts a "more/fewer than N combined goals this half?" button question at `MATCH_START`/`HALF_TIME`, resolves it at the next half/full-time boundary, updates streaks, announces new server records. |
| `handlers.py` | Slash commands (`/start`, `/lang`, `/matches`, `/leaderboard`) registered onto the bot's `CommandTree`. |
| `bot.py` | `python -m app.bot` -- the Discord bot process: registers slash commands, then runs the same live-streaming pipeline as `main.py live` but dispatches to Discord instead of stdout. |
| `replay.py` | Fetches historical scores for a fixture, looks up team names, and re-feeds updates through the same `Detector` at a speed multiplier -- to a single-language `Pundit` printing to stdout, or (`--broadcast`) to `DiscordDispatcher` + `HiLoGame` via a lightweight logged-in `discord.Client`. |
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

## Hi-Lo game notes

- `HALF_TIME` (`Action == "halftime_finalised"`) resolves the 1st-half
  question and opens the 2nd-half one; `MATCH_END` resolves the 2nd half as
  `final total - goals at half`, since the feed's `H2` goals subtotal was
  observed to stay `null` even on records that should have populated it --
  `game.py` tracks combined goals itself off the `GOAL` event stream instead.
- A failed Discord send (question post, resolution post, or a guess button
  callback) is caught and logged, never raised -- one bad send can't crash
  the ingestion pipeline or the bot.

## Tests

```
python -m pytest app/tests -v
```

The end-to-end test hits the real devnet API using `credentials.json` and is
skipped automatically if that file is absent.
