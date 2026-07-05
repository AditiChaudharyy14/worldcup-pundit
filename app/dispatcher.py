"""Fans a fixture's Events out to one Anthropic-backed Pundit per actively
followed language, and posts each language's commentary as a rich embed to
the single designated Discord channel (config.discord_channel_id -- see the
project decision to use one channel for every fixture rather than one per
fixture).

Reuses pundit.Pundit unmodified: one Pundit instance per (fixture_id, lang)
gives "one Claude call per event-batch per language, not per user" for free,
since Pundit already batches a fixture's events into one call -- this just
runs one such instance per language actually being watched, each with its
own sink that posts an embed instead of printing.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from app.config import AppConfig
from app.models import Event
from app.prompts import LANGUAGE_LABELS
from app.pundit import Pundit
from app.storage import Storage

logger = logging.getLogger(__name__)


class DiscordDispatcher:
    def __init__(self, client: discord.Client, config: AppConfig, storage: Storage, channel_id: int):
        self._client = client
        self._config = config
        self._storage = storage
        self._channel_id = channel_id
        self._pundits: dict[tuple[int, str], Pundit] = {}
        self._names: dict[int, tuple[str, str]] = {}
        self._scores: dict[int, dict[int, int]] = {}
        self._pending: set[asyncio.Task] = set()

    def register_fixture(self, fixture_id: int, team1: str, team2: str) -> None:
        self._names[fixture_id] = (team1, team2)
        self._scores.setdefault(fixture_id, {1: 0, 2: 0})

    async def handle_event(self, event: Event) -> None:
        if event.type == "GOAL":
            scores = self._scores.setdefault(event.fixture_id, {1: 0, 2: 0})
            scores[event.payload["participant"]] = event.payload["totalGoals"]
        elif event.type == "MATCH_START":
            self._scores[event.fixture_id] = {1: 0, 2: 0}

        langs = await self._storage.get_active_langs(event.fixture_id)
        if not langs:
            # Nobody's explicitly followed this fixture yet -- show off every
            # language rather than staying silent (also what --broadcast wants).
            langs = set(self._config.settings.default_broadcast_langs)

        for lang in langs:
            await self._pundit_for(event.fixture_id, lang).handle_event(event)

    async def flush_all(self) -> None:
        for pundit in self._pundits.values():
            await pundit.flush_all()

    async def wait_idle(self) -> None:
        """Waits for every in-flight Discord send to finish. Call after
        flush_all() in short-lived processes (e.g. replay --broadcast) so
        posts aren't dropped when the event loop closes right after.
        """
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def aclose(self) -> None:
        for pundit in self._pundits.values():
            await pundit.aclose()

    def _pundit_for(self, fixture_id: int, lang: str) -> Pundit:
        key = (fixture_id, lang)
        pundit = self._pundits.get(key)
        if pundit is None:
            pundit = Pundit(self._config, lang=lang, use_llm=True, sink=self._make_sink(fixture_id, lang))
            team1, team2 = self._names.get(fixture_id, ("Team 1", "Team 2"))
            pundit.register_fixture(fixture_id, team1, team2)
            self._pundits[key] = pundit
        return pundit

    def _make_sink(self, fixture_id: int, lang: str):
        def sink(text: str) -> None:
            task = asyncio.create_task(self._post(fixture_id, lang, text))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

        return sink

    async def _post(self, fixture_id: int, lang: str, text: str) -> None:
        team1, team2 = self._names.get(fixture_id, ("Team 1", "Team 2"))
        score = self._scores.get(fixture_id, {1: 0, 2: 0})
        embed = discord.Embed(title=f"{team1} vs {team2}", description=text, color=discord.Color.blurple())
        embed.add_field(name="Score", value=f"{team1} {score[1]} - {score[2]} {team2}")
        embed.set_footer(text=LANGUAGE_LABELS.get(lang, lang))
        try:
            channel = self._client.get_channel(self._channel_id) or await self._client.fetch_channel(self._channel_id)
            await channel.send(embed=embed)
        except Exception:  # noqa: BLE001 - a failed send must never crash the pipeline
            logger.exception("Failed to post pundit commentary for fixture %s (%s)", fixture_id, lang)
