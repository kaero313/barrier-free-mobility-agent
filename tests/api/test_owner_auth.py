from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from tests.factories import QUESTION
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.api.app import create_app
from barrier_free_mobility_agent.api.dependencies import (
    INTERNAL_TOKEN_HEADER,
    OWNER_ID_HEADER,
)
from barrier_free_mobility_agent.config import (
    ApiOwnerMode,
    AppEnvironment,
    LogFormat,
    Settings,
)
from barrier_free_mobility_agent.domain.identity import OwnerId

_OWNER_ID = "a" * 64
_INTERNAL_TOKEN = "owner-shared-secret-0123456789abcdef"
_REQUEST_ID = "owner-auth-test"


@asynccontextmanager
async def api_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        yield client


def trusted_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env=AppEnvironment.TEST,
        log_format=LogFormat.CONSOLE,
        api_owner_mode=ApiOwnerMode.TRUSTED_HEADER,
        api_owner_shared_secret=_INTERNAL_TOKEN,
        mcp_contract_refresh_seconds=60,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/threads", None),
        ("DELETE", f"/v1/threads/{'b' * 32}", None),
        ("POST", f"/v1/threads/{'b' * 32}/runs", {"question": "테스트 질문"}),
        ("GET", f"/v1/runs/{'c' * 32}", None),
        ("POST", f"/v1/runs/{'c' * 32}/resume", {"answer": "테스트 답변"}),
    ],
)
async def test_every_stateful_route_requires_owner_headers(
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    app = create_app(settings=trusted_settings(), mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        response = await client.request(
            method,
            path,
            json=body,
            headers={"X-Request-ID": _REQUEST_ID},
        )

    assert_owner_authentication_error(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [(OWNER_ID_HEADER, _OWNER_ID)],
        [(INTERNAL_TOKEN_HEADER, _INTERNAL_TOKEN)],
        [(OWNER_ID_HEADER, "a" * 63), (INTERNAL_TOKEN_HEADER, _INTERNAL_TOKEN)],
        [(OWNER_ID_HEADER, "a" * 65), (INTERNAL_TOKEN_HEADER, _INTERNAL_TOKEN)],
        [(OWNER_ID_HEADER, "A" * 64), (INTERNAL_TOKEN_HEADER, _INTERNAL_TOKEN)],
        [(OWNER_ID_HEADER, "g" * 64), (INTERNAL_TOKEN_HEADER, _INTERNAL_TOKEN)],
        [(OWNER_ID_HEADER, _OWNER_ID), (INTERNAL_TOKEN_HEADER, "wrong-token")],
        [
            (OWNER_ID_HEADER, _OWNER_ID),
            (OWNER_ID_HEADER, "b" * 64),
            (INTERNAL_TOKEN_HEADER, _INTERNAL_TOKEN),
        ],
        [
            (OWNER_ID_HEADER, _OWNER_ID),
            (INTERNAL_TOKEN_HEADER, _INTERNAL_TOKEN),
            (INTERNAL_TOKEN_HEADER, _INTERNAL_TOKEN),
        ],
    ],
)
async def test_missing_duplicate_and_invalid_headers_share_safe_401(
    headers: list[tuple[str, str]],
) -> None:
    app = create_app(settings=trusted_settings(), mcp_gateway=FakeMcpGateway())
    request_headers = [("X-Request-ID", _REQUEST_ID), *headers]

    async with api_client(app) as client:
        response = await client.post("/v1/threads", headers=request_headers)

    assert_owner_authentication_error(response)
    assert _OWNER_ID not in response.text
    assert _INTERNAL_TOKEN not in response.text
    assert "wrong-token" not in response.text


@pytest.mark.asyncio
async def test_valid_trusted_headers_are_accepted() -> None:
    app = create_app(settings=trusted_settings(), mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        response = await client.post(
            "/v1/threads",
            headers={
                OWNER_ID_HEADER: _OWNER_ID,
                INTERNAL_TOKEN_HEADER: _INTERNAL_TOKEN,
            },
        )

    assert response.status_code == 201
    assert set(response.json()) == {"schema_version", "request_id", "thread_id", "persistence"}


@pytest.mark.asyncio
async def test_disabled_mode_ignores_untrusted_owner_headers() -> None:
    settings = Settings(
        _env_file=None,
        app_env=AppEnvironment.TEST,
        api_owner_mode=ApiOwnerMode.DISABLED,
    )
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        response = await client.post(
            "/v1/threads",
            headers={
                OWNER_ID_HEADER: "browser-controlled-owner",
                INTERNAL_TOKEN_HEADER: "wrong-token",
            },
        )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_thread_run_and_checkpoint_are_isolated_by_owner() -> None:
    app = create_app(settings=trusted_settings(), mcp_gateway=FakeMcpGateway())
    owner_a = _owner_headers("a")
    owner_b = _owner_headers("b")

    async with api_client(app) as client:
        created = await client.post("/v1/threads", headers=owner_a)
        thread_id = created.json()["thread_id"]

        hidden_thread = await client.post(
            f"/v1/threads/{thread_id}/runs",
            headers=owner_b,
            json={"question": QUESTION},
        )
        started = await client.post(
            f"/v1/threads/{thread_id}/runs",
            headers=owner_a,
            json={"question": QUESTION},
        )
        run_id = started.json()["run_id"]
        hidden_run = await client.get(f"/v1/runs/{run_id}", headers=owner_b)
        hidden_resume = await client.post(
            f"/v1/runs/{run_id}/resume",
            headers=owner_b,
            json={"answer": "다른 사용자의 답변"},
        )
        hidden_delete = await client.delete(f"/v1/threads/{thread_id}", headers=owner_b)
        owner_state = await app.state.container.trip_agent.get_state(
            thread_id,
            owner_id=OwnerId("a" * 64),
        )
        other_state = await app.state.container.trip_agent.get_state(
            thread_id,
            owner_id=OwnerId("b" * 64),
        )
        visible_run = await client.get(f"/v1/runs/{run_id}", headers=owner_a)

    assert hidden_thread.status_code == 404
    assert hidden_thread.json()["error"]["code"] == "THREAD_NOT_FOUND"
    assert hidden_thread.json()["error"]["detail"] == {}
    assert thread_id not in hidden_thread.text
    assert started.status_code == 200
    assert hidden_run.status_code == 404
    assert hidden_run.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert hidden_run.json()["error"]["detail"] == {}
    assert run_id not in hidden_run.text
    assert hidden_resume.status_code == 404
    assert hidden_resume.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert hidden_resume.json()["error"]["detail"] == {}
    assert run_id not in hidden_resume.text
    assert hidden_delete.status_code == 404
    assert hidden_delete.json()["error"]["code"] == "THREAD_NOT_FOUND"
    assert hidden_delete.json()["error"]["detail"] == {}
    assert thread_id not in hidden_delete.text
    assert owner_state
    assert other_state == {}
    assert visible_run.status_code == 200


def _owner_headers(owner_character: str) -> dict[str, str]:
    return {
        OWNER_ID_HEADER: owner_character * 64,
        INTERNAL_TOKEN_HEADER: _INTERNAL_TOKEN,
    }


@pytest.mark.asyncio
async def test_owner_auth_does_not_apply_to_health_routes() -> None:
    app = create_app(settings=trusted_settings(), mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        response = await client.get("/health/live")
        question = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
            headers={"X-Request-ID": _REQUEST_ID},
        )

    assert response.status_code == 200
    assert_owner_authentication_error(question)


def test_openapi_declares_401_without_exposing_internal_headers() -> None:
    app = create_app(settings=trusted_settings(), mcp_gateway=FakeMcpGateway())
    paths = app.openapi()["paths"]
    stateful_operations = (
        ("/v1/threads", "post"),
        ("/v1/threads/{thread_id}", "delete"),
        ("/v1/threads/{thread_id}/runs", "post"),
        ("/v1/runs/{run_id}", "get"),
        ("/v1/runs/{run_id}/resume", "post"),
    )

    for path, method in stateful_operations:
        operation = paths[path][method]
        assert operation["responses"]["401"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
        parameter_names = {
            parameter["name"].lower() for parameter in operation.get("parameters", [])
        }
        assert OWNER_ID_HEADER.lower() not in parameter_names
        assert INTERNAL_TOKEN_HEADER.lower() not in parameter_names

    assert "401" not in paths["/health/live"]["get"]["responses"]
    question_operation = paths["/v1/accessibility/questions"]["post"]
    assert "401" in question_operation["responses"]
    assert OWNER_ID_HEADER.lower() not in {
        parameter["name"].lower() for parameter in question_operation.get("parameters", [])
    }


def assert_owner_authentication_error(response: Response) -> None:
    assert response.status_code == 401
    body = response.json()
    assert body == {
        "schema_version": "2",
        "request_id": _REQUEST_ID,
        "error": {
            "code": "OWNER_AUTHENTICATION_REQUIRED",
            "message": "요청 소유자를 확인할 수 없습니다.",
            "retryable": False,
            "detail": {},
        },
    }
