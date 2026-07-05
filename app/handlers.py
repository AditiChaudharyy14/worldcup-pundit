"""Slash commands, registered onto the bot's CommandTree by bot.py's
setup_hook. Language prefs and fixture follows live in bot.storage (see
storage.py); fixture listings come from bot.txline_client -- the same
TxLineClient used by live/replay mode.
"""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands

from app.models import Fixture
from app.prompts import LANGUAGE_LABELS

logger = logging.getLogger(__name__)


class _LangButton(discord.ui.Button):
    def __init__(self, bot, lang: str):
        super().__init__(label=LANGUAGE_LABELS[lang], style=discord.ButtonStyle.secondary, custom_id=f"lang:{lang}")
        self._bot = bot
        self._lang = lang

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._bot.storage.set_user_lang(str(interaction.user.id), self._lang)
        await interaction.response.edit_message(
            content=f"Commentary language set to {LANGUAGE_LABELS[self._lang]}.", view=None
        )


class _LangView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        for lang in LANGUAGE_LABELS:
            self.add_item(_LangButton(bot, lang))


class _FollowButton(discord.ui.Button):
    def __init__(self, bot, fixture: Fixture, following: bool):
        self._bot = bot
        self._fixture_id = fixture.FixtureId
        self._match_label = f"{fixture.Participant1} vs {fixture.Participant2}"
        super().__init__(
            label=self._render_label(following),
            style=discord.ButtonStyle.success if following else discord.ButtonStyle.secondary,
            custom_id=f"follow:{fixture.FixtureId}",
        )

    def _render_label(self, following: bool) -> str:
        return f"{'✅ ' if following else ''}{self._match_label}"

    async def callback(self, interaction: discord.Interaction) -> None:
        now_following = await self._bot.storage.toggle_subscription(str(interaction.user.id), self._fixture_id)
        self.label = self._render_label(now_following)
        self.style = discord.ButtonStyle.success if now_following else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self.view)


class _MatchesView(discord.ui.View):
    def __init__(self, bot, fixtures: list[Fixture], followed_ids: set[int]):
        super().__init__(timeout=300)
        # 25 components is Discord's hard cap on a single message's view.
        for fixture in fixtures[:25]:
            self.add_item(_FollowButton(bot, fixture, fixture.FixtureId in followed_ids))


def register_commands(tree: app_commands.CommandTree, bot) -> None:
    @tree.command(name="start", description="Pick your pundit commentary language.")
    async def start(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Pick your pundit commentary language:", view=_LangView(bot), ephemeral=True
        )

    @tree.command(name="lang", description="Change your commentary language.")
    async def lang(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Pick your pundit commentary language:", view=_LangView(bot), ephemeral=True
        )

    @tree.command(name="matches", description="Browse today's fixtures and follow/unfollow their commentary.")
    async def matches(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            fixtures = await bot.txline_client.get_fixtures(
                date=dt.datetime.now(dt.timezone.utc).date(),
                competition_id=bot.app_config.settings.default_competition_id,
            )
        except Exception:
            logger.exception("Failed to fetch fixtures for /matches")
            await interaction.followup.send(
                "Couldn't reach TxLINE for today's fixtures -- try again shortly.", ephemeral=True
            )
            return

        if not fixtures:
            await interaction.followup.send("No fixtures found for today.", ephemeral=True)
            return

        external_id = str(interaction.user.id)
        followed_ids = {
            fixture.FixtureId
            for fixture in fixtures
            if await bot.storage.is_subscribed(external_id, fixture.FixtureId)
        }

        await interaction.followup.send(
            "Tap a match to follow/unfollow its commentary:",
            view=_MatchesView(bot, fixtures, followed_ids),
            ephemeral=True,
        )

    @tree.command(name="leaderboard", description="Top Hi-Lo streak game players.")
    async def leaderboard(interaction: discord.Interaction) -> None:
        rows = await bot.storage.leaderboard(limit=10)
        if not rows:
            await interaction.response.send_message("No Hi-Lo streaks yet -- play a round first!", ephemeral=True)
            return
        lines = [
            f"**{i}.** <@{external_id}> -- best streak **{best}** (current {count})"
            for i, (external_id, count, best) in enumerate(rows, start=1)
        ]
        embed = discord.Embed(title="🏆 Hi-Lo Leaderboard", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)
