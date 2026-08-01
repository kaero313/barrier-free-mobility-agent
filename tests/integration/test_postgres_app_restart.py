from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.api.app import create_app
from barrier_free_mobility_agent.config import (
    AppEnvironment,
    LogFormat,
    PersistenceBackend,
    Settings,
)

_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests.",
)


@pytest.mark.asyncio
async def test_app_restart_can_get_and_resume_the_same_interrupted_run() -> None:
    assert _DATABASE_URL is not None
    settings = Settings(
        _env_file=None,
        app_env=AppEnvironment.TEST,
        log_format=LogFormat.CONSOLE,
        persistence_backend=PersistenceBackend.POSTGRES,
        database_url=_DATABASE_URL,
        database_pool_min_size=1,
        database_pool_max_size=2,
        database_auto_setup=True,
        checkpoint_encryption_key="restart-test-checkpoint-key-0001",
        mcp_contract_refresh_seconds=60,
    )

    first_app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())
    async with api_client(first_app) as client:
        created = await client.post("/v1/threads")
        assert created.json()["persistence"] == "durable"
        thread_id = created.json()["thread_id"]
        interrupted = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "홍대입구역에서 삼성역까지 갈 수 있어?"},
        )
        assert interrupted.json()["run_status"] == "interrupted"
        run_id = interrupted.json()["run_id"]
        clarification_id = interrupted.json()["clarification"]["id"]

    wrong_key_app = create_app(
        settings=settings.model_copy(
            update={
                "database_auto_setup": False,
                "checkpoint_encryption_key": SecretStr("x" * 32),
            }
        ),
        mcp_gateway=FakeMcpGateway(),
    )
    with pytest.raises(RuntimeError, match="encryption key does not match"):
        async with wrong_key_app.router.lifespan_context(wrong_key_app):
            pass

    restarted_app = create_app(
        settings=settings.model_copy(update={"database_auto_setup": False}),
        mcp_gateway=FakeMcpGateway(),
    )
    async with api_client(restarted_app) as client:
        restored = await client.get(f"/v1/runs/{run_id}")
        resumed = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={
                "answer": "계단과 에스컬레이터 모두 이용할 수 없어요",
                "clarification_id": clarification_id,
            },
        )
        deleted = await client.delete(f"/v1/threads/{thread_id}")

    assert restored.status_code == 200
    assert restored.json()["run_status"] == "interrupted"
    assert restored.json()["clarification"]["id"] == clarification_id
    assert resumed.status_code == 200
    assert resumed.json()["run_id"] == run_id
    assert resumed.json()["run_status"] == "completed"
    assert deleted.status_code == 204


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
