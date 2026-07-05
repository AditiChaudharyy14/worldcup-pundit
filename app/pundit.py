"""AI pundit: turns Event objects into short fan-facing commentary.

Consumes Event objects (from detector.py, live or replayed) and, per fixture,
batches everything that arrives within a rolling window into ONE Anthropic
call -- so a goal and the odds swing it causes land as a single reaction
instead of two separate messages. The window is measured against each
event's own `ts` (match time), not wall-clock time, so replay at any --speed
multiplier batches identically to live play.

On any failure (API error, network error, malformed response) this falls back
to a plain template message and logs the failure -- the pipeline never stalls
on a bad LLM call. --no-llm skips the API entirely and always uses templates
(for testing the pipeline without API cost).

Never logs or prints the Anthropic API key: it's read once as a SecretStr
from config and only unwrapped via get_secret_value() to construct the client.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from anthropic import AsyncAnthropic

from app.config import AppConfig
from app.models import Event
from app.prompts import FEW_SHOT_EXAMPLES, LANGUAGE_NAMES, SYSTEM_PROMPTS

logger = logging.getLogger(__name__)


class _FixtureContext:
    def __init__(self, fixture_id: int):
        self.fixture_id = fixture_id
        self.team1 = "Team 1"
        self.team2 = "Team 2"
        self.goals: dict[int, int] = {1: 0, 2: 0}
        self.history: list[Event] = []
        self.pending: list[Event] = []
        self.batch_start_ts: int | None = None

    def team(self, participant: int) -> str:
        return self.team1 if participant == 1 else self.team2


class Pundit:
    def __init__(
        self,
        config: AppConfig,
        lang: str,
        use_llm: bool = True,
        sink: Callable[[str], None] = print,
    ):
        if lang not in SYSTEM_PROMPTS:
            raise ValueError(f"Unsupported lang {lang!r}; choose one of {sorted(SYSTEM_PROMPTS)}")
        self._config = config
        self._lang = lang
        self._use_llm = use_llm
        self._sink = sink
        self._fixtures: dict[int, _FixtureContext] = {}
        self._client: AsyncAnthropic | None = None
        if use_llm:
            if config.settings.anthropic_api_key is None:
                raise ValueError(
                    "ANTHROPIC_API_KEY is not set. Add it to .env, or pass use_llm=False / --no-llm."
                )
            self._client = AsyncAnthropic(api_key=config.settings.anthropic_api_key.get_secret_value())

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    def register_fixture(self, fixture_id: int, team1: str, team2: str) -> None:
        ctx = self._context(fixture_id)
        ctx.team1 = team1
        ctx.team2 = team2

    def _context(self, fixture_id: int) -> _FixtureContext:
        return self._fixtures.setdefault(fixture_id, _FixtureContext(fixture_id))

    async def handle_event(self, event: Event) -> None:
        ctx = self._context(event.fixture_id)
        self._apply_facts(ctx, event)

        window_ms = self._config.settings.commentary_batch_window_seconds * 1000
        if ctx.pending and ctx.batch_start_ts is not None and event.ts - ctx.batch_start_ts > window_ms:
            await self._flush(ctx)

        if not ctx.pending:
            ctx.batch_start_ts = event.ts
        ctx.pending.append(event)

    async def flush_all(self) -> None:
        for ctx in self._fixtures.values():
            if ctx.pending:
                await self._flush(ctx)

    def _apply_facts(self, ctx: _FixtureContext, event: Event) -> None:
        if event.type == "GOAL":
            ctx.goals[event.payload["participant"]] = event.payload["totalGoals"]

    async def _flush(self, ctx: _FixtureContext) -> None:
        events = ctx.pending
        ctx.pending = []
        ctx.batch_start_ts = None

        if self._use_llm:
            text = await self._generate(ctx, events)
        else:
            text = self._template(ctx, events)

        ctx.history.extend(events)
        ctx.history = ctx.history[-3:]
        self._sink(text)

    def _describe(self, ctx: _FixtureContext, event: Event) -> str:
        if event.type == "GOAL":
            p = event.payload["participant"]
            return f"GOAL by {ctx.team(p)} (now {ctx.goals[1]}-{ctx.goals[2]})"
        if event.type == "RED_CARD":
            return f"RED_CARD for {ctx.team(event.payload['participant'])}"
        if event.type == "MATCH_START":
            return "MATCH_START"
        if event.type == "HALF_TIME":
            return f"HALF_TIME (score at the break {ctx.team1} {ctx.goals[1]}-{ctx.goals[2]} {ctx.team2})"
        if event.type == "MATCH_END":
            return f"MATCH_END (final score {ctx.team1} {ctx.goals[1]}-{ctx.goals[2]} {ctx.team2})"
        if event.type == "ODDS_SWING":
            pl = event.payload
            sign = "+" if pl["toPct"] >= pl["fromPct"] else "-"
            delta = abs(pl["deltaPct"])
            return (
                f"ODDS_SWING {pl['priceName']} {pl['fromPct']:.1f}% -> "
                f"{pl['toPct']:.1f}% ({sign}{delta:.1f}pp)"
            )
        return event.type

    def _build_user_prompt(self, ctx: _FixtureContext, events: list[Event]) -> str:
        lines = [
            f"Match: {ctx.team1} vs {ctx.team2}",
            f"Current score: {ctx.team1} {ctx.goals[1]} - {ctx.goals[2]} {ctx.team2}",
        ]
        if ctx.history:
            lines.append("Recent events: " + "; ".join(self._describe(ctx, e) for e in ctx.history))
        lines.append("New event(s) to react to now: " + "; ".join(self._describe(ctx, e) for e in events))
        lines.append(
            f"Write ONE short pundit reaction (in {LANGUAGE_NAMES[self._lang]}) to the new event(s) only."
        )
        return "\n".join(lines)

    async def _generate(self, ctx: _FixtureContext, events: list[Event]) -> str:
        assert self._client is not None
        messages = []
        for user_turn, assistant_turn in FEW_SHOT_EXAMPLES[self._lang]:
            messages.append({"role": "user", "content": user_turn})
            messages.append({"role": "assistant", "content": assistant_turn})
        messages.append({"role": "user", "content": self._build_user_prompt(ctx, events)})

        try:
            response = await self._client.messages.create(
                model=self._config.settings.pundit_model,
                max_tokens=self._config.settings.pundit_max_tokens,
                system=SYSTEM_PROMPTS[self._lang],
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
                messages=messages,
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("model refused to generate commentary")
            text = "".join(block.text for block in response.content if block.type == "text").strip()
            if not text:
                raise RuntimeError("empty response content")
            return text
        except Exception as exc:  # noqa: BLE001 - any failure must degrade gracefully
            logger.warning("Pundit LLM call failed for fixture %s (%s): %s", ctx.fixture_id, self._lang, exc)
            return self._template(ctx, events)

    def _template(self, ctx: _FixtureContext, events: list[Event]) -> str:
        return " ".join(self._template_one(ctx, e) for e in events)

    def _template_one(self, ctx: _FixtureContext, event: Event) -> str:
        if event.type == "GOAL":
            p = event.payload["participant"]
            return f"GOAL! {ctx.team1} {ctx.goals[1]}-{ctx.goals[2]} {ctx.team2} ({ctx.team(p)} scores)."
        if event.type == "RED_CARD":
            return f"Red card for {ctx.team(event.payload['participant'])}."
        if event.type == "MATCH_START":
            return f"Kickoff! {ctx.team1} vs {ctx.team2} is underway."
        if event.type == "HALF_TIME":
            return f"Half time: {ctx.team1} {ctx.goals[1]}-{ctx.goals[2]} {ctx.team2}."
        if event.type == "MATCH_END":
            return f"Full time: {ctx.team1} {ctx.goals[1]}-{ctx.goals[2]} {ctx.team2}."
        if event.type == "ODDS_SWING":
            pl = event.payload
            return f"Odds move: {pl['priceName']} {pl['fromPct']:.1f}% -> {pl['toPct']:.1f}%."
        return event.type
