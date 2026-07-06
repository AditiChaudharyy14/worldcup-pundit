"""Replay harness: pulls historical score updates for a fixture and replays
them through the exact same Detector used by main.py's live mode, at a
configurable speed multiplier, so the resulting Events -- and the pundit
commentary generated from them -- are indistinguishable from what live mode
would have produced.

CLI:
    python -m app.replay --fixture 18179549 --speed 10
    python -m app.replay --fixture 18179549 --speed 1000 --lang ne
    python -m app.replay --fixture 18179549 --speed 1000 --no-llm   # template-only, no API cost
    python -m app.replay --fixture 18179549 --speed 60 --broadcast  # push into the real Discord bot
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import logging
import sys

import discord

from app.config import AppConfig, load_config
from app.detector import Detector
from app.dispatcher import DiscordDispatcher
from app.game import HiLoGame
from app.models import Event
from app.pundit import Pundit
from app.storage import Storage
from app.txline_client import TxLineClient

logger = logging.getLogger(__name__)

_EPOCH = dt.date(1970, 1, 1)

# Some fixtures' historical feed includes a stray metadata record (observed:
# "coverage_update") posted *days* before the real pre-match buildup starts --
# uncapped, that single gap would make pacing sleep for hours regardless of
# --speed. The largest legitimate in-match gap observed across sampled
# fixtures was ~14 minutes, so 30 minutes only ever clips that one artifact.
_MAX_RAW_GAP_SECONDS = 1800.0


async def _consume(
    queue: "asyncio.Queue[Event]",
    stop: asyncio.Event,
    pundit_sink: Pundit | DiscordDispatcher,
    game: HiLoGame | None,
) -> list[Event]:
    events: list[Event] = []
    while not (stop.is_set() and queue.empty()):
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        events.append(event)
        await pundit_sink.handle_event(event)
        if game is not None:
            await game.handle_event(event)
    return events


async def _lookup_fixture_names(
    client: TxLineClient, config: AppConfig, fixture_id: int, start_time_ms: int
) -> tuple[str, str]:
    epoch_day = start_time_ms // 86_400_000
    date = _EPOCH + dt.timedelta(days=epoch_day)
    try:
        fixtures = await client.get_fixtures(date=date, competition_id=config.settings.default_competition_id)
        for f in fixtures:
            if f.FixtureId == fixture_id:
                return f.Participant1, f.Participant2
    except Exception as exc:  # noqa: BLE001 - names are a nice-to-have, never fatal
        logger.warning("Could not look up fixture names for %s: %s", fixture_id, exc)
    return "Team 1", "Team 2"


async def run_replay(
    fixture_id: int,
    speed: float,
    lang: str = "en",
    use_llm: bool = True,
    broadcast: bool = False,
    config: AppConfig | None = None,
) -> list[Event]:
    """Fetch `fixture_id`'s historical score updates and replay them through a
    fresh Detector at `speed`x original pacing, printing pundit commentary
    (via Pundit) as events are produced -- or, with `broadcast=True`, posting
    it (fanned out per actively-followed language, plus the Hi-Lo game) into
    the real Discord bot/channel instead. Returns every Event emitted (a
    fixture already fully replayed once will emit none the second time, per
    detector.py's idempotency contract).
    """
    config = config or load_config()

    async with Storage(config.settings.sqlite_path, config.settings.database_url) as storage:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        detector = Detector(storage, queue, config.settings)

        discord_client: discord.Client | None = None
        discord_login_task: asyncio.Task | None = None
        game: HiLoGame | None = None

        if broadcast:
            if config.settings.discord_bot_token is None or config.settings.discord_channel_id is None:
                raise ValueError(
                    "--broadcast requires DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID in the repo-root .env."
                )
            channel_id = config.settings.discord_channel_id
            discord_client = discord.Client(intents=discord.Intents.default())
            # login() awaits its setup hook (initialises wait_until_ready()'s
            # internal Event) before returning; only then is it safe to
            # background connect() and wait on readiness -- doing both via a
            # single backgrounded start() races wait_until_ready() against
            # that setup.
            await discord_client.login(config.settings.discord_bot_token.get_secret_value())
            discord_login_task = asyncio.create_task(discord_client.connect())
            await discord_client.wait_until_ready()
            pundit_sink: Pundit | DiscordDispatcher = DiscordDispatcher(discord_client, config, storage, channel_id)
            game = HiLoGame(discord_client, config, storage, channel_id)
        else:
            pundit_sink = Pundit(config, lang=lang, use_llm=use_llm)

        stop = asyncio.Event()
        consumer_task = asyncio.create_task(_consume(queue, stop, pundit_sink, game))

        try:
            async with TxLineClient(config) as client:
                updates = await client.get_historical_scores(fixture_id)
                updates.sort(key=lambda u: u.Ts)
                if updates:
                    team1, team2 = await _lookup_fixture_names(client, config, fixture_id, updates[0].StartTime)
                    pundit_sink.register_fixture(fixture_id, team1, team2)
                    if game is not None:
                        game.register_fixture(fixture_id, team1, team2)

            logger.info(
                "Replaying %d score updates for fixture %d at %sx (lang=%s, llm=%s, broadcast=%s)",
                len(updates), fixture_id, speed, lang, use_llm, broadcast,
            )

            # Pre-kickoff records (lineups, venue, weather, ...) carry no Events
            # and their own spacing is irrelevant to the demo, so race through
            # them instantly; only pace realistically once the match is on,
            # which is also what keeps --speed meaningful regardless of how
            # far in advance a fixture's pre-match metadata was posted.
            kickoff_ts = next((u.Ts for u in updates if u.Action == "kickoff"), None)
            in_match = kickoff_ts is None
            previous_ts: int | None = None
            for update in updates:
                if not in_match:
                    if update.Ts >= kickoff_ts:
                        in_match = True
                        previous_ts = update.Ts
                elif previous_ts is not None and speed > 0:
                    raw_gap = min((update.Ts - previous_ts) / 1000.0, _MAX_RAW_GAP_SECONDS)
                    delay = raw_gap / speed
                    if delay > 0:
                        await asyncio.sleep(delay)
                    previous_ts = update.Ts
                else:
                    previous_ts = update.Ts
                await detector.feed_score(update)

            stop.set()
            events = await consumer_task
            await pundit_sink.flush_all()
            if isinstance(pundit_sink, DiscordDispatcher):
                await pundit_sink.wait_idle()
            return events
        finally:
            await pundit_sink.aclose()
            if discord_client is not None:
                await discord_client.close()
            if discord_login_task is not None:
                discord_login_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await discord_login_task


def main() -> None:
    # Commentary can be in Devanagari script (ne/hi); Windows terminals default
    # to cp1252, which raises UnicodeEncodeError on those characters.
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr.encoding is not None and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Replay a finished fixture through the TxLINE detector + pundit pipeline.")
    parser.add_argument("--fixture", type=int, required=True, help="FixtureId to replay")
    parser.add_argument("--speed", type=float, default=10.0, help="Speed multiplier (e.g. 10 = 10x real time)")
    parser.add_argument("--lang", choices=["en", "ne", "hi"], default="en", help="Commentary language (ignored with --broadcast)")
    parser.add_argument("--no-llm", action="store_true", help="Use template messages only, skip the Anthropic API")
    parser.add_argument(
        "--broadcast",
        action="store_true",
        help="Push commentary + the Hi-Lo game into the real Discord bot/channel instead of printing "
        "(posts every actively-followed language, or all three if nobody's following yet)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    events = asyncio.run(
        run_replay(args.fixture, args.speed, lang=args.lang, use_llm=not args.no_llm, broadcast=args.broadcast)
    )
    logger.info("Replay complete: %d event(s) emitted", len(events))


if __name__ == "__main__":
    main()
