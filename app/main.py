"""CLI entrypoint.

    python -m app.main live --lang en                          # today's fixtures -> streams -> pundit commentary
    python -m app.main replay --fixture X --speed 10 --lang ne  # delegates to replay.py
    python -m app.main serve                                    # bot + web status page, one process (Phase 5)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import logging
import os

import uvicorn

from app.bot import PunditBot
from app.config import load_config
from app.detector import Detector
from app.pundit import Pundit
from app.replay import run_replay
from app.state import STATE
from app.storage import Storage
from app.txline_client import TxLineClient
from app.web import create_app

logger = logging.getLogger(__name__)


async def _periodic_flush(pundit: Pundit, interval_seconds: float) -> None:
    """Safety net for live mode: a batch with no follow-up event would otherwise
    never flush (unlike replay, a live stream has no natural end to flush at).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        await pundit.flush_all()


async def run_live(lang: str, use_llm: bool) -> None:
    config = load_config()

    async with Storage(config.settings.sqlite_path, config.settings.database_url) as storage, TxLineClient(config) as client:
        fixtures = await client.get_fixtures(
            date=dt.datetime.now(dt.timezone.utc).date(),
            competition_id=config.settings.default_competition_id,
        )
        fixture_ids = [f.FixtureId for f in fixtures]
        logger.info("Tracking %d fixture(s) today: %s", len(fixture_ids), fixture_ids)

        queue: asyncio.Queue = asyncio.Queue()
        detector = Detector(storage, queue, config.settings)
        pundit = Pundit(config, lang=lang, use_llm=use_llm)
        for f in fixtures:
            pundit.register_fixture(f.FixtureId, f.Participant1, f.Participant2)

        async def consume_events() -> None:
            while True:
                event = await queue.get()
                await pundit.handle_event(event)

        async def consume_scores() -> None:
            async for update in client.stream_scores(fixture_ids):
                await detector.feed_score(update)

        async def consume_odds() -> None:
            async for update in client.stream_odds(fixture_ids):
                await detector.feed_odds(update)

        try:
            await asyncio.gather(
                consume_events(),
                consume_scores(),
                consume_odds(),
                _periodic_flush(pundit, config.settings.commentary_batch_window_seconds),
            )
        finally:
            await pundit.aclose()


async def run_serve() -> None:
    """Single deployable unit (Phase 5): TxLINE ingestion + detector + pundit
    + Discord bot + FastAPI status page, all as asyncio tasks in one process.

    Whichever of {bot, web server} stops first (crash, or a SIGTERM from the
    host asking us to shut down) triggers a coordinated shutdown of the
    other -- plain asyncio.gather() would instead leave the survivor running
    forever, or propagate a raw CancelledError instead of the real cause.
    """
    config = load_config()
    if config.settings.discord_bot_token is None:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Add it to .env (see .env.example).")

    async with Storage(config.settings.sqlite_path, config.settings.database_url) as storage:
        bot = PunditBot(config, storage)
        STATE.bot = bot

        web_app = create_app(storage, config)
        port = int(os.environ.get("PORT", "8000"))
        uv_config = uvicorn.Config(web_app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(uv_config)

        bot_task = asyncio.create_task(bot.start(config.settings.discord_bot_token.get_secret_value()))
        web_task = asyncio.create_task(server.serve())
        logger.info("Serving status page on 0.0.0.0:%d and starting the Discord bot.", port)

        done, pending = await asyncio.wait({bot_task, web_task}, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await bot.close()

        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc


def main() -> None:
    parser = argparse.ArgumentParser(description="TxLINE World Cup fan-experience ingestion core.")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    live_parser = subparsers.add_parser("live", help="Stream today's fixtures live and print pundit commentary.")
    live_parser.add_argument("--lang", choices=["en", "ne", "hi"], default="en")
    live_parser.add_argument("--no-llm", action="store_true")

    replay_parser = subparsers.add_parser("replay", help="Replay a finished fixture's history through the detector.")
    replay_parser.add_argument("--fixture", type=int, required=True)
    replay_parser.add_argument("--speed", type=float, default=10.0)
    replay_parser.add_argument("--lang", choices=["en", "ne", "hi"], default="en")
    replay_parser.add_argument("--no-llm", action="store_true")
    replay_parser.add_argument("--broadcast", action="store_true", help="Push into the real Discord bot/channel")

    subparsers.add_parser("serve", help="Run bot + ingestion + web status page together, one process (deployment).")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.mode == "live":
        try:
            asyncio.run(run_live(args.lang, not args.no_llm))
        except KeyboardInterrupt:
            pass
    elif args.mode == "replay":
        asyncio.run(
            run_replay(args.fixture, args.speed, lang=args.lang, use_llm=not args.no_llm, broadcast=args.broadcast)
        )
    elif args.mode == "serve":
        try:
            asyncio.run(run_serve())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
