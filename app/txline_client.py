"""Async TxLINE API client.

Endpoints (verified against a live devnet call on 2026-07-05, see
models.py docstring for docs-vs-reality notes):
  GET /fixtures/snapshot                  -> JSON array of Fixture
  GET /scores/historical/{fixtureId}       -> SSE-formatted body of ScoreUpdate
                                              (NOT a plain JSON array, despite docs)
  GET /scores/stream                       -> SSE stream of ScoreUpdate
  GET /odds/stream                         -> SSE stream of OddsUpdate
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import AppConfig
from app.models import Fixture, OddsUpdate, ScoreUpdate
from app.txline_auth import TxLineAuth

logger = logging.getLogger(__name__)

_EPOCH = dt.date(1970, 1, 1)


def _epoch_day(date: dt.date) -> int:
    return (date - _EPOCH).days


def _parse_sse_blocks(text: str) -> list[dict[str, Any]]:
    """Parse `data: {...}\\nid: N\\n\\n` blocks into JSON objects.

    Used both for the mislabelled /scores/historical response and, if ever
    needed, for pre-buffered SSE text. Ignores blocks with no `data:` line
    (e.g. bare heartbeats) and skips lines that fail to parse as JSON.
    """
    records: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        data_lines = [
            line[len("data:"):].strip()
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            records.append(json.loads("\n".join(data_lines)))
        except json.JSONDecodeError:
            logger.warning("Skipping unparsable SSE block: %.200s", block)
    return records


class TxLineClient:
    def __init__(self, config: AppConfig):
        self._config = config
        self._auth = TxLineAuth(config)
        self._client = httpx.AsyncClient(
            base_url=config.api_base_url,
            auth=self._auth,
            timeout=config.settings.http_timeout_seconds,
            headers={"Accept-Encoding": "gzip"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "TxLineClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def get_fixtures(
        self, date: dt.date | None = None, competition_id: int | None = None
    ) -> list[Fixture]:
        params: dict[str, int] = {}
        if competition_id is not None:
            params["competitionId"] = competition_id
        if date is not None:
            params["startEpochDay"] = _epoch_day(date)
        response = await self._client.get("/fixtures/snapshot", params=params)
        response.raise_for_status()
        return [Fixture.model_validate(item) for item in response.json()]

    async def get_historical_scores(self, fixture_id: int) -> list[ScoreUpdate]:
        response = await self._client.get(f"/scores/historical/{fixture_id}")
        response.raise_for_status()
        records = _parse_sse_blocks(response.text)
        return [ScoreUpdate.model_validate(rec) for rec in records]

    async def stream_scores(self, fixture_ids: list[int] | None = None) -> AsyncIterator[ScoreUpdate]:
        async for raw in self._stream_sse("/scores/stream", fixture_ids):
            yield ScoreUpdate.model_validate(raw)

    async def stream_odds(self, fixture_ids: list[int] | None = None) -> AsyncIterator[OddsUpdate]:
        async for raw in self._stream_sse("/odds/stream", fixture_ids):
            yield OddsUpdate.model_validate(raw)

    async def _stream_sse(
        self, path: str, fixture_ids: list[int] | None
    ) -> AsyncIterator[dict[str, Any]]:
        """Long-lived SSE consumer with resume-by-Last-Event-ID and exponential
        backoff reconnect. The API only documents filtering by a single
        `fixtureId` query param, so for >1 id we stream unfiltered and filter
        client-side (also done for a single id, defensively).
        """
        params: dict[str, int] = {}
        if fixture_ids and len(fixture_ids) == 1:
            params["fixtureId"] = fixture_ids[0]
        fixture_id_set = set(fixture_ids) if fixture_ids else None

        last_event_id: str | None = None
        backoff = self._config.settings.reconnect_initial_backoff_seconds
        max_backoff = self._config.settings.reconnect_max_backoff_seconds

        while True:
            headers = {"Accept-Encoding": "gzip"}
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id
            try:
                async with self._client.stream(
                    "GET", path, params=params, headers=headers
                ) as response:
                    response.raise_for_status()
                    backoff = self._config.settings.reconnect_initial_backoff_seconds

                    event_type: str | None = None
                    data_lines: list[str] = []
                    event_id: str | None = None

                    async for line in response.aiter_lines():
                        if line == "":
                            if data_lines:
                                if event_id:
                                    last_event_id = event_id
                                if event_type != "heartbeat":
                                    try:
                                        obj = json.loads("\n".join(data_lines))
                                    except json.JSONDecodeError:
                                        logger.warning("Skipping unparsable SSE event on %s", path)
                                        obj = None
                                    if obj is not None and (
                                        fixture_id_set is None or obj.get("FixtureId") in fixture_id_set
                                    ):
                                        yield obj
                            event_type, data_lines, event_id = None, [], None
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[len("data:"):].strip())
                        elif line.startswith("event:"):
                            event_type = line[len("event:"):].strip()
                        elif line.startswith("id:"):
                            event_id = line[len("id:"):].strip()
            except (httpx.HTTPError, httpx.StreamError) as exc:
                logger.warning("%s stream dropped (%s), reconnecting in %.1fs", path, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
