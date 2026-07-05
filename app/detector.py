"""Turns raw ScoreUpdate/OddsUpdate records into Event objects on an
asyncio.Queue.

feed_score/feed_odds are pure with respect to input ordering: feed the same
sequence of updates for a fixture and you get the same Events, regardless of
whether that sequence arrived from a live SSE stream (main.py) or from
replay.py re-playing historical records at a speed multiplier. Idempotency
(no duplicate Events for the same underlying happening, even across two full
runs) is enforced by storage.Storage.processed_events, checked in _emit.

See models.py's module docstring for why match-state transitions are read
from `Action` ("kickoff" / "game_finalised") rather than the GameState field,
and why Score changes are only trusted on records where Confirmed is not
explicitly False.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from app.config import Settings
from app.models import Event, OddsUpdate, ScoreUpdate
from app.storage import Storage

logger = logging.getLogger(__name__)

# The flagship demarginalised "Stable Price" line -- ODDS_SWING is computed
# against this one canonical line so swings aren't muddied by comparing
# across different bookmakers/lines that may move independently.
_CANONICAL_BOOKMAKER = "TXLineStablePriceDemargined"


class _FixtureState:
    def __init__(self) -> None:
        self.goals: dict[int, int] = {1: 0, 2: 0}
        self.red_cards: dict[int, int] = {1: 0, 2: 0}
        self.match_started = False
        self.half_time = False
        self.match_ended = False
        self.last_goal_ts: int | None = None


class Detector:
    def __init__(self, storage: Storage, queue: "asyncio.Queue[Event]", settings: Settings):
        self._storage = storage
        self._queue = queue
        self._settings = settings
        self._fixtures: dict[int, _FixtureState] = {}
        # (fixture_id, market_period, price_name) -> deque[(ts, pct)] within the window
        self._odds_windows: dict[tuple[int, str, str], deque[tuple[int, float]]] = {}
        self._swing_active: set[tuple[int, str, str]] = set()

    def _state(self, fixture_id: int) -> _FixtureState:
        return self._fixtures.setdefault(fixture_id, _FixtureState())

    async def _emit(self, event: Event) -> None:
        if await self._storage.try_mark_processed(event):
            await self._queue.put(event)

    async def feed_score(self, update: ScoreUpdate) -> None:
        state = self._state(update.FixtureId)

        if update.Action == "kickoff" and not state.match_started and update.Confirmed is not False:
            state.match_started = True
            await self._emit(
                Event(
                    type="MATCH_START",
                    fixture_id=update.FixtureId,
                    ts=update.Ts,
                    payload={
                        "participant1Id": update.Participant1Id,
                        "participant2Id": update.Participant2Id,
                    },
                    dedupe_key=f"{update.FixtureId}:MATCH_START",
                )
            )

        # Observed once per fixture (Action "halftime_finalised") -- the game.py
        # Hi-Lo game uses this to resolve the 1st-half question and open the 2nd.
        if update.Action == "halftime_finalised" and not state.half_time:
            state.half_time = True
            await self._emit(
                Event(
                    type="HALF_TIME",
                    fixture_id=update.FixtureId,
                    ts=update.Ts,
                    payload={
                        "participant1Id": update.Participant1Id,
                        "participant2Id": update.Participant2Id,
                    },
                    dedupe_key=f"{update.FixtureId}:HALF_TIME",
                )
            )

        if update.Action == "game_finalised" and not state.match_ended:
            state.match_ended = True
            await self._emit(
                Event(
                    type="MATCH_END",
                    fixture_id=update.FixtureId,
                    ts=update.Ts,
                    payload={
                        "participant1Id": update.Participant1Id,
                        "participant2Id": update.Participant2Id,
                    },
                    dedupe_key=f"{update.FixtureId}:MATCH_END",
                )
            )

        # Only trust Score on records that aren't an explicitly-unconfirmed
        # proposal -- a proposed-then-discarded goal must never bump the count.
        if update.Score is not None and update.Confirmed is not False:
            participant_blocks = (
                (1, update.Score.Participant1),
                (2, update.Score.Participant2),
            )
            for participant, block in participant_blocks:
                if block is None or block.Total is None:
                    continue

                new_goals = block.Total.Goals or 0
                if new_goals > state.goals[participant]:
                    state.goals[participant] = new_goals
                    state.last_goal_ts = update.Ts
                    await self._emit(
                        Event(
                            type="GOAL",
                            fixture_id=update.FixtureId,
                            ts=update.Ts,
                            payload={"participant": participant, "totalGoals": new_goals},
                            dedupe_key=f"{update.FixtureId}:GOAL:{participant}:{new_goals}",
                        )
                    )

                new_reds = block.Total.RedCards or 0
                if new_reds > state.red_cards[participant]:
                    state.red_cards[participant] = new_reds
                    await self._emit(
                        Event(
                            type="RED_CARD",
                            fixture_id=update.FixtureId,
                            ts=update.Ts,
                            payload={"participant": participant, "totalRedCards": new_reds},
                            dedupe_key=f"{update.FixtureId}:RED_CARD:{participant}:{new_reds}",
                        )
                    )

    async def feed_odds(self, update: OddsUpdate) -> None:
        if update.Bookmaker != _CANONICAL_BOOKMAKER:
            return
        if not update.MarketPeriod or not update.PriceNames or not update.Pct:
            return

        state = self._state(update.FixtureId)
        window_ms = self._settings.odds_swing_window_seconds * 1000
        dedupe_ms = self._settings.goal_odds_dedupe_seconds * 1000

        for price_name, pct_str in zip(update.PriceNames, update.Pct):
            if pct_str == "NA":
                continue
            try:
                pct = float(pct_str)
            except ValueError:
                continue

            key = (update.FixtureId, update.MarketPeriod, price_name)
            window = self._odds_windows.setdefault(key, deque())
            window.append((update.Ts, pct))
            cutoff = update.Ts - window_ms
            while window and window[0][0] < cutoff:
                window.popleft()

            pcts = [p for _, p in window]
            delta = max(pcts) - min(pcts)

            if delta < self._settings.odds_swing_threshold_pct:
                self._swing_active.discard(key)
                continue

            if key in self._swing_active:
                continue  # already reported this swing; wait for it to reset
            self._swing_active.add(key)

            if state.last_goal_ts is not None and update.Ts - state.last_goal_ts <= dedupe_ms:
                # A GOAL already told this story in the last 60s -- don't also fire ODDS_SWING.
                continue

            await self._emit(
                Event(
                    type="ODDS_SWING",
                    fixture_id=update.FixtureId,
                    ts=update.Ts,
                    payload={
                        "marketPeriod": update.MarketPeriod,
                        "priceName": price_name,
                        "fromPct": round(window[0][1], 3),
                        "toPct": round(pct, 3),
                        "deltaPct": round(delta, 3),
                    },
                    dedupe_key=(
                        f"{update.FixtureId}:ODDS_SWING:{update.MarketPeriod}:"
                        f"{price_name}:{update.MessageId}"
                    ),
                )
            )
