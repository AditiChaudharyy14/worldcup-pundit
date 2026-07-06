"""Tiny in-process registry so app.web (the FastAPI status page) can read
live state from app.bot's Discord client. Everything runs in one process /
event loop under `python -m app.main serve` (see main.py), so a plain shared
object is enough -- no IPC needed.
"""

from __future__ import annotations

import datetime as dt
from typing import Any


class AppState:
    def __init__(self) -> None:
        self.started_at = dt.datetime.now(dt.timezone.utc)
        # Set to the running PunditBot instance by main.py's serve().
        self.bot: Any = None

    @property
    def uptime_seconds(self) -> float:
        return (dt.datetime.now(dt.timezone.utc) - self.started_at).total_seconds()

    @property
    def discord_online(self) -> bool:
        return bool(self.bot is not None and self.bot.is_ready() and not self.bot.is_closed())


STATE = AppState()
