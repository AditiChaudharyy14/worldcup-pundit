"""CLI entrypoint.

    python -m app.main live                          # today's World Cup fixtures -> live streams -> events
    python -m app.main replay --fixture X --speed 10  # delegates to replay.py
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging

from app.config import load_config
from app.detector import Detector
from app.models import Event
from app.replay import run_replay
from app.storage import Storage
from app.txline_client import TxLineClient

logger = logging.getLogger(__name__)


async def _print_events(queue: "asyncio.Queue[Event]") -> None:
    while True:
        event = await queue.get()
        print(event.to_log_line())


async def run_live() -> None:
    config = load_config()

    async with Storage(config.settings.sqlite_path) as storage, TxLineClient(config) as client:
        fixtures = await client.get_fixtures(
            date=dt.datetime.now(dt.timezone.utc).date(),
            competition_id=config.settings.default_competition_id,
        )
        fixture_ids = [f.FixtureId for f in fixtures]
        logger.info("Tracking %d fixture(s) today: %s", len(fixture_ids), fixture_ids)

        queue: asyncio.Queue[Event] = asyncio.Queue()
        detector = Detector(storage, queue, config.settings)

        async def consume_scores() -> None:
            async for update in client.stream_scores(fixture_ids):
                await detector.feed_score(update)

        async def consume_odds() -> None:
            async for update in client.stream_odds(fixture_ids):
                await detector.feed_odds(update)

        await asyncio.gather(
            _print_events(queue),
            consume_scores(),
            consume_odds(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="TxLINE World Cup fan-experience ingestion core.")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    subparsers.add_parser("live", help="Stream today's fixtures live and print detected events.")

    replay_parser = subparsers.add_parser("replay", help="Replay a finished fixture's history through the detector.")
    replay_parser.add_argument("--fixture", type=int, required=True)
    replay_parser.add_argument("--speed", type=float, default=10.0)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.mode == "live":
        try:
            asyncio.run(run_live())
        except KeyboardInterrupt:
            pass
    elif args.mode == "replay":
        asyncio.run(run_replay(args.fixture, args.speed))


if __name__ == "__main__":
    main()
