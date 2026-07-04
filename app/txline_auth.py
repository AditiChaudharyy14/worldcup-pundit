"""Auth for TxLINE requests: Authorization Bearer JWT + X-Api-Token headers,
with automatic guest-JWT refresh-and-retry-once on 401.

Implemented as an httpx.Auth so every request made through a client
constructed with `auth=TxLineAuth(config)` gets this behaviour for free,
including inside `AsyncClient.stream(...)` used for SSE.

Never logs the jwt or api token: both are held as pydantic SecretStr and only
unwrapped via get_secret_value() at the point of building request headers.
"""

from __future__ import annotations

import asyncio

import httpx
from pydantic import SecretStr

from app.config import AppConfig


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
        async with httpx.AsyncClient(timeout=self._config.settings.http_timeout_seconds) as client:
            response = await client.post(f"{self._config.api_origin}/auth/guest/start")
            response.raise_for_status()
            self._jwt = SecretStr(response.json()["token"])
