"""Settings + onboarding credentials loading.

credentials.json is produced by ../onboarding/subscribe.ts. jwt/apiToken are
held as SecretStr so accidental logging (str(config), repr(config), f"{config}")
never leaks them -- callers must explicitly call .get_secret_value().
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent


class Credentials(BaseModel):
    walletPublicKey: str
    txSig: str
    jwt: SecretStr
    apiToken: SecretStr
    serviceLevelId: int
    weeks: int
    leagues: list[int]
    apiBaseUrl: str
    activatedAt: str


class Settings(BaseSettings):
    """Tunables. Override via env vars prefixed TXLINE_APP_ or a .env file."""

    model_config = SettingsConfigDict(env_prefix="TXLINE_APP_", env_file=".env", extra="ignore")

    credentials_path: Path = REPO_ROOT / "onboarding" / "credentials.json"
    sqlite_path: Path = APP_DIR / "txline.db"

    # World Cup 2026, as observed on every real fixture returned during onboarding probing.
    default_competition_id: int = 72

    http_timeout_seconds: float = 20.0
    reconnect_initial_backoff_seconds: float = 1.0
    reconnect_max_backoff_seconds: float = 30.0

    # Detector tuning (see detector.py for rationale).
    odds_swing_threshold_pct: float = 7.0
    odds_swing_window_seconds: int = 600
    goal_odds_dedupe_seconds: int = 60

    # Pundit (see pundit.py). Plain env var name (not TXLINE_APP_-prefixed) so it
    # matches the Anthropic SDK's own ANTHROPIC_API_KEY convention. Optional at
    # config-load time -- only Pundit(use_llm=True) requires it, so replay/live
    # with --no-llm (or code that never touches the pundit) works without it.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    pundit_model: str = "claude-sonnet-4-6"
    pundit_max_tokens: int = 150
    commentary_batch_window_seconds: int = 60

    # Discord bot (see bot.py / handlers.py / game.py). Plain env var names
    # (not TXLINE_APP_-prefixed) since they're the platform's own convention.
    # Only required when actually running the bot or replay --broadcast.
    discord_bot_token: SecretStr | None = Field(default=None, validation_alias="DISCORD_BOT_TOKEN")
    # Single channel used for every fixture's pundit/Hi-Lo posts (see the
    # project decision to keep one designated channel rather than one per
    # fixture, for this single-test-server hackathon build).
    discord_channel_id: int | None = Field(default=None, validation_alias="DISCORD_CHANNEL_ID")
    # Optional: if set, slash commands sync instantly to this guild instead of
    # up to an hour globally -- set this to "WC Pundit Test"'s guild ID.
    discord_guild_id: int | None = Field(default=None, validation_alias="DISCORD_GUILD_ID")

    # Languages broadcast for a fixture nobody has explicitly followed yet
    # (e.g. fresh --broadcast demo runs) -- shows off all three at once.
    default_broadcast_langs: list[str] = ["en", "ne", "hi"]

    # Hi-Lo streak game (see game.py): combined-goals-per-half line for the
    # "more or fewer" question.
    hilo_goals_line: float = 2.0


class AppConfig(BaseModel):
    settings: Settings
    credentials: Credentials

    @property
    def api_base_url(self) -> str:
        """e.g. https://txline-dev.txodds.com/api -- for /fixtures, /odds, /scores calls."""
        return self.credentials.apiBaseUrl

    @property
    def api_origin(self) -> str:
        """e.g. https://txline-dev.txodds.com -- for /auth/guest/start."""
        return self.credentials.apiBaseUrl.removesuffix("/api")


def load_config(settings: Settings | None = None) -> AppConfig:
    settings = settings or Settings()
    if not settings.credentials_path.exists():
        raise FileNotFoundError(
            f"No credentials at {settings.credentials_path}. "
            "Run onboarding first: npx ts-node onboarding/subscribe.ts"
        )
    credentials = Credentials.model_validate_json(settings.credentials_path.read_text())
    return AppConfig(settings=settings, credentials=credentials)
