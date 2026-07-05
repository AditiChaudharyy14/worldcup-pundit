"""CLI entrypoint.

    python -m app.main live --lang en                          # today's fixtures -> streams -> pundit commentary
    python -m app.main replay --fixture X --speed 10 --lang ne  # delegates to replay.py
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging

from app.config import load_config
from app.detector import Detector
from app.pundit import Pundit
from app.replay import run_replay
from app.storage import Storage
from app.txline_client import TxLineClient

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

    async with Storage(config.settings.sqlite_path) as storage, TxLineClient(config) as client:
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


if __name__ == "__main__":
    main()
