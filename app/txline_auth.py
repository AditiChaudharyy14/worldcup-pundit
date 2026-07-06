"""Auth for TxLINE requests: Authorization Bearer JWT + X-Api-Token headers,
with automatic guest-JWT refresh-and-retry-once on 401.

Implemented as an httpx.Auth so every request made through a client
constructed with `auth=TxLineAuth(config)` gets this behaviour for free,
including inside `AsyncClient.stream(...)` used for SSE.

Only the guest JWT is refreshed here -- verified live against the real
devnet API that /token/activate (which mints the X-Api-Token) is one-time-use
per on-chain txSig, so it can't be part of an automatic refresh loop (see
wallet_auth.py's docstring). The API token is long-lived for the whole paid
subscription; only the JWT is expected to need refreshing, and refreshing it
needs no signature at all -- just a fresh POST /auth/guest/start.

Never logs the jwt or api token: both are held as pydantic SecretStr and only
unwrapped via get_secret_value() at the point of building request headers.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import SecretStr

from app.config import AppConfig

logger = logging.getLogger(__name__)

_MAX_REFRESH_ATTEMPTS = 5


class TxLineAuth(httpx.Auth):
    requires_response_body = True

    def __init__(self, config: AppConfig):
        self._config = config
        self._jwt: SecretStr = config.credentials.jwt
        self._api_token: SecretStr = config.credentials.apiToken
        self._lock = asyncio.Lock()

    def _apply_headers(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = f"Bearer {self._jwt.get_secret_value()}"
        request.headers["X-Api-Token"] = self._api_token.get_secret_value()

    async def async_auth_flow(self, request: httpx.Request):
        self._apply_headers(request)
        response = yield request

        if response.status_code == 401:
            await response.aread()
            async with self._lock:
                await self._refresh_jwt()
            self._apply_headers(request)
            yield request

    async def _refresh_jwt(self) -> None:
        """Retries with exponential backoff since /auth/guest/start can be
        transiently flaky. Never raises -- on repeated failure, the retried
        request above just 401s again with the stale jwt, and the caller's
        own reconnect/backoff (see txline_client.py's SSE streams) keeps the
        process alive to try the whole thing again later, so a bad refresh
        can never crash the pipeline, only delay recovery.
        """
        backoff = self._config.settings.reconnect_initial_backoff_seconds
        max_backoff = self._config.settings.reconnect_max_backoff_seconds

        for attempt in range(1, _MAX_REFRESH_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self._config.settings.http_timeout_seconds) as client:
                    response = await client.post(f"{self._config.api_origin}/auth/guest/start")
                    response.raise_for_status()
                    self._jwt = SecretStr(response.json()["token"])
                logger.info("Refreshed TxLINE guest JWT (attempt %d/%d).", attempt, _MAX_REFRESH_ATTEMPTS)
                return
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                logger.warning(
                    "TxLINE guest JWT refresh attempt %d/%d failed: %s", attempt, _MAX_REFRESH_ATTEMPTS, exc
                )
                if attempt >= _MAX_REFRESH_ATTEMPTS:
                    logger.error("TxLINE guest JWT refresh failed after %d attempts; giving up for this 401.", attempt)
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
