"""Pydantic models built from REAL API responses observed on 2026-07-05 against
https://txline-dev.txodds.com (devnet), not from the OpenAPI spec at
https://txline.txodds.com/docs/docs.yaml. Where the two disagree, a comment
below says so -- this file follows reality.

Known docs vs. reality mismatches:
  * /api/scores/historical/{fixtureId} is documented as `application/json`
    returning an array of `Scores` objects with lowerCamelCase properties
    (fixtureId, gameState, startTime, ...). In reality it returns an SSE-style
    body (repeated `data: {...}\\nid: N\\n\\n` blocks) with PascalCase
    properties (FixtureId, GameState, StartTime, ...), i.e. the same shape as
    the documented `/api/scores/stream` events. txline_client.py parses it as
    SSE regardless of the declared content-type.
  * The `GameState` field is NOT a consistent match-state signal across
    endpoints: on /fixtures/snapshot it's an int (e.g. 3 for a finished
    fixture); on /odds/snapshot it was observed null; on
    /scores/historical it is a constant string ("scheduled") for every
    record in a fixture regardless of actual match progress. detector.py
    therefore ignores GameState entirely and derives match-start/end from the
    `Action` field ("kickoff" / "game_finalised"), which was observed to
    change correctly over the course of a real match.
  * /api/odds/snapshot/{fixtureId} matched its documented `OddsPayload`
    schema exactly (PascalCase, as documented).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Fixture(BaseModel):
    """From GET /fixtures/snapshot. Matches the documented Fixture schema."""

    model_config = ConfigDict(extra="allow")

    Ts: int
    StartTime: int
    Competition: str
    CompetitionId: int
    FixtureGroupId: int
    Participant1Id: int
    Participant1: str
    Participant2Id: int
    Participant2: str
    FixtureId: int
    Participant1IsHome: bool
    # Integer status code, observed values include 1 (upcoming) and 3 (finished).
    # Not present on every fixture returned.
    GameState: int | None = None


class PeriodScore(BaseModel):
    """One period's (H1/HT/H2/Total/...) worth of soccer scoring stats.

    All fields are optional: a period sub-object only carries the keys that
    have actually happened so far (e.g. no RedCards key until a red card
    occurs), never zeros.
    """

    model_config = ConfigDict(extra="allow")

    Goals: int | None = None
    YellowCards: int | None = None
    RedCards: int | None = None
    Corners: int | None = None


class ParticipantScore(BaseModel):
    model_config = ConfigDict(extra="allow")

    Total: PeriodScore | None = None
    HT: PeriodScore | None = None
    H1: PeriodScore | None = None
    H2: PeriodScore | None = None


class ScoreBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    Participant1: ParticipantScore | None = None
    Participant2: ParticipantScore | None = None


class ScoreUpdate(BaseModel):
    """One record from /scores/historical/{fixtureId} or /scores/stream.

    The feed is an amendment log, not a clean event log: the same logical
    event (a goal, a card) is typically posted 2-3 times under the same `Id`
    as `Confirmed` goes False -> True and `Data` gets filled in, and can be
    retracted entirely via a later record with Action == "action_discarded"
    referencing that Id (Score then reverts). detector.py handles this by
    only trusting Score changes on records where Confirmed is not False, and
    by keying off the monotonically-tracked Score totals rather than the
    `Action == "goal"` tag directly -- this was verified necessary against a
    real fixture (18179549) that contained exactly one such discarded goal.
    """

    model_config = ConfigDict(extra="allow")

    FixtureId: int
    StartTime: int
    FixtureGroupId: int
    CompetitionId: int
    Participant1IsHome: bool
    Participant2Id: int
    Participant1Id: int
    Action: str
    Id: int
    Ts: int
    Seq: int
    StatusId: int | None = None
    Type: str | None = None
    Confirmed: bool | None = None
    Clock: dict[str, Any] | None = None
    Score: ScoreBlock | None = None
    Data: dict[str, Any] | None = None
    Stats: dict[str, Any] | None = None
    # Unreliable -- see module docstring. Kept for completeness/debugging only.
    GameState: str | None = None


class OddsUpdate(BaseModel):
    """From /odds/snapshot/{fixtureId} or /odds/stream. Matches the documented
    OddsPayload schema exactly (PascalCase, as observed).
    """

    model_config = ConfigDict(extra="allow")

    FixtureId: int
    MessageId: str
    Ts: int
    Bookmaker: str
    BookmakerId: int
    SuperOddsType: str
    GameState: str | None = None
    InRunning: bool
    MarketParameters: str | None = None
    MarketPeriod: str | None = None
    PriceNames: list[str] = []
    Prices: list[int] = []
    # Percent strings formatted "NN.NNN" (3 dp), or "NA" for quarter handicap lines.
    Pct: list[str] = []


EventType = Literal["GOAL", "RED_CARD", "MATCH_START", "MATCH_END", "ODDS_SWING"]


class Event(BaseModel):
    """What detector.py puts on its asyncio.Queue, and what replay.py must
    reproduce byte-for-byte given the same historical input.
    """

    type: EventType
    fixture_id: int
    ts: int
    payload: dict[str, Any] = {}
    # Stable across repeated runs over the same source data -- this is the
    # idempotency key checked against storage.processed_events.
    dedupe_key: str

    def to_log_line(self) -> str:
        return json.dumps(
            {"type": self.type, "fixture_id": self.fixture_id, "ts": self.ts, **self.payload},
            sort_keys=True,
        )
