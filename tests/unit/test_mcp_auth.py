from __future__ import annotations

import asyncio
from base64 import b64decode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from barrier_free_mobility_agent.integrations.mcp.auth import (
    OidcClientCredentialsAuth,
    OidcTokenError,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@asynccontextmanager
async def resource_client(
    auth: OidcClientCredentialsAuth,
    handler: httpx.AsyncBaseTransport,
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(auth=auth, transport=handler) as client:
        yield client
    await auth.aclose()


@pytest.mark.asyncio
async def test_oidc_auth_uses_basic_client_auth_and_caches_token() -> None:
    token_calls = 0
    resource_headers: list[str] = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        token_calls += 1
        encoded_credentials = request.headers["Authorization"].removeprefix("Basic ")
        assert b64decode(encoded_credentials).decode() == "mobility-agent:client-secret"
        form = parse_qs(request.content.decode())
        assert form == {"grant_type": ["client_credentials"], "scope": ["mcp.read"]}
        assert b"client-secret" not in request.content
        return httpx.Response(
            200,
            json={"access_token": "access-token", "token_type": "Bearer", "expires_in": 60},
        )

    def resource_handler(request: httpx.Request) -> httpx.Response:
        resource_headers.append(request.headers["Authorization"])
        return httpx.Response(200)

    auth = OidcClientCredentialsAuth(
        token_url="https://identity.example.com/oauth/token",
        client_id="mobility-agent",
        client_secret=SecretStr("client-secret"),
        scope="mcp.read",
        transport=httpx.MockTransport(token_handler),
    )
    async with resource_client(auth, httpx.MockTransport(resource_handler)) as client:
        await client.get("https://mcp.example.com/health")
        await client.get("https://mcp.example.com/health")
        assert "access-token" not in repr(auth.__dict__)

    assert token_calls == 1
    assert resource_headers == ["Bearer access-token", "Bearer access-token"]
    assert "client-secret" not in repr(auth)


@pytest.mark.asyncio
async def test_oidc_auth_refreshes_before_expiry() -> None:
    clock = MutableClock()
    token_calls = 0

    def token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        token_calls += 1
        return httpx.Response(
            200,
            json={
                "access_token": f"token-{token_calls}",
                "token_type": "bearer",
                "expires_in": 100,
            },
        )

    seen: list[str] = []

    def resource_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200)

    auth = OidcClientCredentialsAuth(
        token_url="https://identity.example.com/oauth/token",
        client_id="mobility-agent",
        client_secret=SecretStr("client-secret"),
        refresh_leeway_seconds=30,
        transport=httpx.MockTransport(token_handler),
        clock=clock,
    )
    async with resource_client(auth, httpx.MockTransport(resource_handler)) as client:
        await client.get("https://mcp.example.com/health")
        clock.now = 89
        await client.get("https://mcp.example.com/health")
        clock.now = 90
        await client.get("https://mcp.example.com/health")

    assert token_calls == 2
    assert seen == ["Bearer token-1", "Bearer token-1", "Bearer token-2"]


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_token_refresh() -> None:
    token_calls = 0

    async def token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        token_calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={"access_token": "shared-token", "token_type": "Bearer", "expires_in": 60},
        )

    auth = OidcClientCredentialsAuth(
        token_url="https://identity.example.com/oauth/token",
        client_id="mobility-agent",
        client_secret=SecretStr("client-secret"),
        transport=httpx.MockTransport(token_handler),
    )
    resource_transport = httpx.MockTransport(lambda request: httpx.Response(200))
    async with resource_client(auth, resource_transport) as client:
        await asyncio.gather(*(client.get("https://mcp.example.com/health") for _ in range(20)))

    assert token_calls == 1


@pytest.mark.asyncio
async def test_unauthorized_response_invalidates_without_replaying_request() -> None:
    token_calls = 0
    resource_calls = 0

    def token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        token_calls += 1
        return httpx.Response(
            200,
            json={
                "access_token": f"token-{token_calls}",
                "token_type": "Bearer",
                "expires_in": 60,
            },
        )

    def resource_handler(request: httpx.Request) -> httpx.Response:
        nonlocal resource_calls
        resource_calls += 1
        return httpx.Response(401 if resource_calls == 1 else 200)

    auth = OidcClientCredentialsAuth(
        token_url="https://identity.example.com/oauth/token",
        client_id="mobility-agent",
        client_secret=SecretStr("client-secret"),
        transport=httpx.MockTransport(token_handler),
    )
    async with resource_client(auth, httpx.MockTransport(resource_handler)) as client:
        first = await client.post("https://mcp.example.com/mcp", content=b"tool-call")
        assert first.status_code == 401
        assert resource_calls == 1
        second = await client.post("https://mcp.example.com/mcp", content=b"tool-call")

    assert second.status_code == 200
    assert token_calls == 2
    assert resource_calls == 2


@pytest.mark.asyncio
async def test_invalid_token_response_fails_without_exposing_payload() -> None:
    auth = OidcClientCredentialsAuth(
        token_url="https://identity.example.com/oauth/token",
        client_id="mobility-agent",
        client_secret=SecretStr("client-secret"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"access_token": "leaked-token"})
        ),
    )
    async with resource_client(
        auth,
        httpx.MockTransport(lambda request: httpx.Response(200)),
    ) as client:
        with pytest.raises(OidcTokenError) as exc_info:
            await client.get("https://mcp.example.com/health")

    assert str(exc_info.value) == "token_response_invalid"
    assert "leaked-token" not in str(exc_info.value)
