"""Discord bot runner: wires the same TxLINE ingestion pipeline used by
main.py's live mode to a real Discord server. Pundit commentary (fanned out
per actively-followed language by dispatcher.DiscordDispatcher) and the
Hi-Lo streak game (game.HiLoGame) post to one designated channel
(config.discord_channel_id); users manage their language and followed
fixtures via the slash commands in handlers.py.

    python -m app.bot

Requires DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID in the repo-root .env, and
the bot already invited to the target server with the Message Content
intent enabled in the developer portal.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import discord
from discord.ext import commands

from app.config import AppConfig, load_config
from app.detector import Detector
from app.dispatcher import DiscordDispatcher
from app.game import HiLoGame
from app.handlers import register_commands
from app.storage import Storage
from app.txline_client import TxLineClient

logger = logging.getLogger(__name__)


class PunditBot(commands.Bot):
    def __init__(self, config: AppConfig, storage: Storage):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.app_config = config
        self.storage = storage
        self.txline_client = TxLineClient(config)
        self.dispatcher: DiscordDispatcher | None = None
        self.game: HiLoGame | None = None
        self._live_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        register_commands(self.tree, self)
        guild_id = self.app_config.settings.discord_guild_id
        if guild_id is not None:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Synced slash commands to guild %s (instant).", guild_id)
        else:
            await self.tree.sync()
            logger.info("Synced slash commands globally (can take up to an hour to propagate).")

        channel_id = self.app_config.settings.discord_channel_id
        if channel_id is not None:
            self.dispatcher = DiscordDispatcher(self, self.app_config, self.storage, channel_id)
            self.game = HiLoGame(self, self.app_config, self.storage, channel_id)
            await self._reattach_open_hilo_views()

        self._live_task = asyncio.create_task(self._run_live())

    async def _reattach_open_hilo_views(self) -> None:
        """Re-registers (Client.add_view()) every still-unresolved Hi-Lo
        question's buttons -- regardless of whether bot.py or a replay
        --broadcast run originally posted it (both share this same SQLite
        db) -- so a question that was still open when its posting process
        last stopped keeps accepting clicks once this process is up.
        """
        assert self.game is not None
        questions = await self.storage.list_open_questions()
        for question in questions:
            view = self.game.build_view(question["id"], question["line"])
            self.add_view(view, message_id=int(question["message_id"]))
        if questions:
            logger.info("Reattached %d open Hi-Lo question view(s).", len(questions))

    async def _run_live(self) -> None:
        await self.wait_until_ready()
        if self.dispatcher is None or self.game is None:
            logger.warning("DISCORD_CHANNEL_ID is not set -- pundit/Hi-Lo posting is disabled; slash commands still work.")
            return

        queue: asyncio.Queue = asyncio.Queue()
        detector = Detector(self.storage, queue, self.app_config.settings)

        fixtures = await self.txline_client.get_fixtures(
            date=dt.datetime.now(dt.timezone.utc).date(),
            competition_id=self.app_config.settings.default_competition_id,
        )
        fixture_ids = [f.FixtureId for f in fixtures]
        logger.info("Bot tracking %d fixture(s) today: %s", len(fixture_ids), fixture_ids)
        for f in fixtures:
            self.dispatcher.register_fixture(f.FixtureId, f.Participant1, f.Participant2)
            self.game.register_fixture(f.FixtureId, f.Participant1, f.Participant2)

        async def consume_events() -> None:
            while True:
                event = await queue.get()
                await self.dispatcher.handle_event(event)
                await self.game.handle_event(event)

        async def consume_scores() -> None:
            async for update in self.txline_client.stream_scores(fixture_ids):
                await detector.feed_score(update)

        async def consume_odds() -> None:
            async for update in self.txline_client.stream_odds(fixture_ids):
                await detector.feed_odds(update)

        async def periodic_flush() -> None:
            while True:
                await asyncio.sleep(self.app_config.settings.commentary_batch_window_seconds)
                await self.dispatcher.flush_all()

        try:
            await asyncio.gather(consume_events(), consume_scores(), consume_odds(), periodic_flush())
        except Exception:
            logger.exception("Live ingestion pipeline crashed; bot stays up for slash commands.")

    async def close(self) -> None:
        if self._live_task is not None:
            self._live_task.cancel()
        if self.dispatcher is not None:
            await self.dispatcher.aclose()
        await self.txline_client.aclose()
        await super().close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    if config.settings.discord_bot_token is None:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Add it to the repo-root .env (see .env.example).")

    async def run() -> None:
        async with Storage(config.settings.sqlite_path, config.settings.database_url) as storage:
            bot = PunditBot(config, storage)
            try:
                await bot.start(config.settings.discord_bot_token.get_secret_value())
            finally:
                await bot.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
