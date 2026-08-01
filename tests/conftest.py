from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

import pytest

from barrier_free_mobility_agent.config import AppEnvironment, LogFormat, Settings


def pytest_asyncio_loop_factories(
    config: pytest.Config,
    item: pytest.Item,
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Run async tests on the selector loop required by psycopg on Windows."""

    return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env=AppEnvironment.TEST,
        log_format=LogFormat.CONSOLE,
        mcp_contract_refresh_seconds=60,
    )
