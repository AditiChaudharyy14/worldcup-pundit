"""Replay harness: pulls historical score updates for a fixture and replays
them through the exact same Detector used by main.py's live mode, at a
configurable speed multiplier, so the resulting Events are indistinguishable
from what live mode would have produced.

CLI:
    python -m app.replay --fixture 18179549 --speed 10
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.config import AppConfig, load_config
from app.detector import Detector
from app.models import Event
from app.storage import Storage
from app.txline_client import TxLineClient

logger = logging.getLogger(__name__)


async def _consume(queue: "asyncio.Queue[Event]", stop: asyncio.Event) -> list[Event]:
    events: list[Event] = []
    while not (stop.is_set() and queue.empty()):
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        events.append(event)
        print(event.to_log_line())
    return events


async def run_replay(
    fixture_id: int, speed: float, config: AppConfig | None = None
) -> list[Event]:
    """Fetch `fixture_id`'s historical score updates and replay them through a
    fresh Detector at `speed`x original pacing. Returns every Event emitted
    (empty list entries already seen via storage.processed_events are simply
    not re-emitted, per detector.py's idempotency contract).
    """
    config = config or load_config()

    async with Storage(config.settings.sqlite_path) as storage:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        detector = Detector(storage, queue, config.settings)
        stop = asyncio.Event()
        consumer_task = asyncio.create_task(_consume(queue, stop))

        async with TxLineClient(config) as client:
            updates = await client.get_historical_scores(fixture_id)

        updates.sort(key=lambda u: u.Ts)
        logger.info("Replaying %d score updates for fixture %d at %sx", len(updates), fixture_id, speed)

        previous_ts: int | None = None
        for update in updates:
            if previous_ts is not None and speed > 0:
                delay = (update.Ts - previous_ts) / 1000.0 / speed
                if delay > 0:
                    await asyncio.sleep(delay)
            previous_ts = update.Ts
            await detector.feed_score(update)

        stop.set()
        events = await consumer_task
        return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a finished fixture through the TxLINE detector pipeline.")
    parser.add_argument("--fixture", type=int, required=True, help="FixtureId to replay")
    parser.add_argument("--speed", type=float, default=10.0, help="Speed multiplier (e.g. 10 = 10x real time)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    events = asyncio.run(run_replay(args.fixture, args.speed))
    logger.info("Replay complete: %d event(s) emitted", len(events))


if __name__ == "__main__":
    main()
