"""Hi-Lo streak mini-game: at MATCH_START and HALF_TIME, posts a "more or
fewer than N combined goals this half?" question with two buttons to the
designated Discord channel, resolves it at the next half/full-time boundary
against the actual score, and updates each guesser's streak in SQLite
(storage.py's hilo_questions/hilo_guesses/streaks tables).

Tracks goals off the same Event stream the pundit sees (see dispatcher.py)
rather than reading detector.py's internal state -- mirrors how pundit.py
keeps its own running score independently of the detector.
"""

from __future__ import annotations

import logging

import discord

from app.config import AppConfig
from app.models import Event
from app.storage import Storage

logger = logging.getLogger(__name__)

_PERIOD_LABELS = {"H1": "1st half", "H2": "2nd half"}


class _GuessButton(discord.ui.Button):
    def __init__(self, game: "HiLoGame", question_id: int, guess: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"hilo:{question_id}:{guess}")
        self._game = game
        self._question_id = question_id
        self._guess = guess

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._game.handle_guess(interaction, self._question_id, self._guess)


class _GuessView(discord.ui.View):
    def __init__(self, game: "HiLoGame", question_id: int, line: float):
        super().__init__(timeout=None)
        self.add_item(_GuessButton(game, question_id, "more", f"More than {line:g}"))
        self.add_item(_GuessButton(game, question_id, "fewer", f"Fewer than {line:g}"))


class HiLoGame:
    def __init__(self, client: discord.Client, config: AppConfig, storage: Storage, channel_id: int):
        self._client = client
        self._config = config
        self._storage = storage
        self._channel_id = channel_id
        self._names: dict[int, tuple[str, str]] = {}
        self._goals_at_half: dict[int, int] = {}
        self._total_goals: dict[int, dict[int, int]] = {}

    def register_fixture(self, fixture_id: int, team1: str, team2: str) -> None:
        self._names[fixture_id] = (team1, team2)
        self._total_goals.setdefault(fixture_id, {1: 0, 2: 0})

    def build_view(self, question_id: int, line: float) -> discord.ui.View:
        """Reconstructs the guess buttons for an already-posted question --
        used both when first posting it and to re-register with
        Client.add_view() on startup (see bot.py) so buttons on a question
        that was still open when the process last stopped keep working.
        """
        return _GuessView(self, question_id, line)

    async def handle_event(self, event: Event) -> None:
        if event.type == "GOAL":
            goals = self._total_goals.setdefault(event.fixture_id, {1: 0, 2: 0})
            goals[event.payload["participant"]] = event.payload["totalGoals"]
        elif event.type == "MATCH_START":
            self._total_goals[event.fixture_id] = {1: 0, 2: 0}
            await self._open_question(event.fixture_id, "H1")
        elif event.type == "HALF_TIME":
            combined = sum(self._total_goals.get(event.fixture_id, {1: 0, 2: 0}).values())
            self._goals_at_half[event.fixture_id] = combined
            await self._resolve_period(event.fixture_id, "H1", combined)
            await self._open_question(event.fixture_id, "H2")
        elif event.type == "MATCH_END":
            combined = sum(self._total_goals.get(event.fixture_id, {1: 0, 2: 0}).values())
            second_half = combined - self._goals_at_half.get(event.fixture_id, 0)
            await self._resolve_period(event.fixture_id, "H2", second_half)

    async def _open_question(self, fixture_id: int, period: str) -> None:
        line = self._config.settings.hilo_goals_line
        question_id = await self._storage.create_question(fixture_id, period, line, self._channel_id)
        if question_id is None:
            return  # already open -- never double-post (e.g. a reprocessed event)

        team1, team2 = self._names.get(fixture_id, ("Team 1", "Team 2"))
        embed = discord.Embed(
            title="🎯 Hi-Lo",
            description=(
                f"**{team1} vs {team2}** -- more or fewer than **{line:g}** combined "
                f"goals in the {_PERIOD_LABELS[period]}?"
            ),
            color=discord.Color.purple(),
        )
        view = self.build_view(question_id, line)
        try:
            channel = self._client.get_channel(self._channel_id) or await self._client.fetch_channel(self._channel_id)
            message = await channel.send(embed=embed, view=view)
            await self._storage.set_question_message(question_id, message.id)
        except Exception:  # noqa: BLE001 - a failed send must never crash the pipeline
            logger.exception("Failed to post Hi-Lo question for fixture %s (%s)", fixture_id, period)

    async def handle_guess(self, interaction: discord.Interaction, question_id: int, guess: str) -> None:
        try:
            recorded = await self._storage.record_guess(question_id, str(interaction.user.id), guess)
            # Opportunistic, not load-bearing for the guess itself -- only
            # used later by the web status page's leaderboard.
            await self._storage.set_user_display_name(str(interaction.user.id), interaction.user.display_name)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record Hi-Lo guess for question %s", question_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong recording your guess.", ephemeral=True)
            return

        if recorded:
            await interaction.response.send_message(f"Locked in: **{guess}** 👍", ephemeral=True)
        else:
            await interaction.response.send_message("You've already guessed on this one!", ephemeral=True)

    async def _resolve_period(self, fixture_id: int, period: str, actual_goals: int) -> None:
        question = await self._storage.get_open_question(fixture_id, period)
        if question is None:
            return
        outcome = "more" if actual_goals > question["line"] else "fewer"
        guesses = await self._storage.resolve_question(question["id"], outcome)

        result_lines = []
        new_records = []
        for external_id, guess in guesses:
            correct = guess == outcome
            count, best, is_record = await self._storage.bump_streak(external_id, correct)
            mark = "✅" if correct else "❌"
            result_lines.append(f"{mark} <@{external_id}> -- streak now {count} (best {best})")
            if is_record:
                new_records.append((external_id, best))

        team1, team2 = self._names.get(fixture_id, ("Team 1", "Team 2"))
        embed = discord.Embed(
            title="🎯 Hi-Lo result",
            description=(
                f"**{team1} vs {team2}** -- the {_PERIOD_LABELS[period]} had **{actual_goals:g}** "
                f"combined goal(s) ({outcome} than {question['line']:g})."
            ),
            color=discord.Color.purple(),
        )
        if result_lines:
            embed.add_field(name="Results", value="\n".join(result_lines), inline=False)

        try:
            channel = self._client.get_channel(self._channel_id) or await self._client.fetch_channel(self._channel_id)
            if question["message_id"]:
                try:
                    old_message = await channel.fetch_message(int(question["message_id"]))
                    await old_message.edit(view=None)
                except discord.NotFound:
                    pass
            await channel.send(embed=embed)
            for external_id, best in new_records:
                await channel.send(f"🔥 **New server record!** <@{external_id}> just hit a Hi-Lo streak of **{best}**!")
        except Exception:  # noqa: BLE001 - a failed send must never crash the pipeline
            logger.exception("Failed to post Hi-Lo result for fixture %s (%s)", fixture_id, period)
