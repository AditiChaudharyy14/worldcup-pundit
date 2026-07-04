"""End-to-end test: replay a real, finished World Cup fixture from the live
devnet API through the actual Detector, and check the emitted GOAL count
against the fixture's real final score.

Requires ../onboarding/credentials.json (skipped if absent) and network
access to the devnet API. /scores/historical/{fixtureId} only serves fixtures
between ~6 hours and ~2 weeks in the past (per TxLINE's OpenAPI spec), so
FIXTURE_ID below may need bumping to a more recent finished fixture if this
test starts skipping/failing on "no records" long after it was written.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings, load_config
from app.detector import Detector
from app.storage import Storage
from app.txline_client import TxLineClient

# Colombia vs Ghana, World Cup group stage. Chosen because its real feed
# contains a *discarded* phantom second goal (proposed, never confirmed) --
# a good check that the detector doesn't over-count.
FIXTURE_ID = 18179549


def _credentials_available() -> bool:
    try:
        load_config()
        return True
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    not _credentials_available(),
    reason="onboarding/credentials.json not found; run onboarding first",
)


async def test_replay_goal_count_matches_final_score():
    config = load_config(Settings(sqlite_path=":memory:"))

    async with TxLineClient(config) as client:
        updates = await client.get_historical_scores(FIXTURE_ID)

    if not updates:
        pytest.skip(f"No historical records for fixture {FIXTURE_ID} (outside the API's time window)")

    updates.sort(key=lambda u: u.Ts)

    last_confirmed_score = None
    for update in updates:
        if update.Score is not None and update.Confirmed is not False:
            last_confirmed_score = update.Score
    assert last_confirmed_score is not None, "fixture never reported a Score"

    expected_total_goals = 0
    for participant_score in (last_confirmed_score.Participant1, last_confirmed_score.Participant2):
        if participant_score is not None and participant_score.Total is not None:
            expected_total_goals += participant_score.Total.Goals or 0

    async with Storage(":memory:") as storage:
        queue: asyncio.Queue = asyncio.Queue()
        detector = Detector(storage, queue, config.settings)

        for update in updates:
            await detector.feed_score(update)

        first_pass_events = []
        while not queue.empty():
            first_pass_events.append(queue.get_nowait())

        goal_events = [e for e in first_pass_events if e.type == "GOAL"]
        assert len(goal_events) == expected_total_goals

        # Idempotency: replaying the same fixture again must not emit
        # duplicate events -- storage.processed_events should suppress them all.
        for update in updates:
            await detector.feed_score(update)

        second_pass_events = []
        while not queue.empty():
            second_pass_events.append(queue.get_nowait())

        assert second_pass_events == []
