from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import httpx
from pydantic import SecretStr


class OidcTokenError(RuntimeError):
    """A safe, payload-free OIDC token acquisition failure."""


@dataclass(frozen=True, slots=True)
class _CachedToken:
    access_token: SecretStr
    refresh_at: float


class OidcClientCredentialsAuth(httpx.Auth):
    """Concurrency-safe OAuth 2.0 client-credentials authentication for MCP."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        scope: str | None = None,
        timeout_seconds: float = 3.0,
        refresh_leeway_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token_url = token_url
        self._scope = scope.strip() if scope and scope.strip() else None
        self._refresh_leeway_seconds = refresh_leeway_seconds
        self._clock = clock
        self._refresh_lock = asyncio.Lock()
        self._cached_token: _CachedToken | None = None
        self._token_client = httpx.AsyncClient(
            auth=httpx.BasicAuth(
                quote_plus(client_id, safe=""),
                quote_plus(client_secret.get_secret_value(), safe=""),
            ),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )

    async def async_auth_flow(
        self,
        request: httpx.Request,
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._get_access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == httpx.codes.UNAUTHORIZED:
            await self._invalidate(token)

    async def aclose(self) -> None:
        self._cached_token = None
        await self._token_client.aclose()

    async def _get_access_token(self) -> str:
        cached = self._cached_token
        if cached is not None and self._clock() < cached.refresh_at:
            return cached.access_token.get_secret_value()

        async with self._refresh_lock:
            cached = self._cached_token
            if cached is not None and self._clock() < cached.refresh_at:
                return cached.access_token.get_secret_value()
            refreshed = await self._request_token()
            self._cached_token = refreshed
            return refreshed.access_token.get_secret_value()

    async def _request_token(self) -> _CachedToken:
        form = {"grant_type": "client_credentials"}
        if self._scope is not None:
            form["scope"] = self._scope

        try:
            response = await self._token_client.post(self._token_url, data=form)
            response.raise_for_status()
        except httpx.HTTPError:
            raise OidcTokenError("token_endpoint_unavailable") from None

        try:
            payload: Any = response.json()
        except ValueError:
            raise OidcTokenError("token_response_invalid") from None
        if not isinstance(payload, dict):
            raise OidcTokenError("token_response_invalid")

        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token.strip()
            or not isinstance(token_type, str)
            or token_type.strip().casefold() != "bearer"
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not math.isfinite(float(expires_in))
            or expires_in <= 0
        ):
            raise OidcTokenError("token_response_invalid")

        lifetime = float(expires_in)
        effective_leeway = min(self._refresh_leeway_seconds, lifetime * 0.1)
        return _CachedToken(
            access_token=SecretStr(access_token.strip()),
            refresh_at=self._clock() + lifetime - effective_leeway,
        )

    async def _invalidate(self, rejected_token: str) -> None:
        async with self._refresh_lock:
            cached = self._cached_token
            if cached is not None and cached.access_token.get_secret_value() == rejected_token:
                self._cached_token = None
