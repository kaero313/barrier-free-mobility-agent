from __future__ import annotations

from typing import Any

import pytest
from psycopg.rows import dict_row

import barrier_free_mobility_agent.persistence.worker_health as worker_health


class _FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.query = ""
        self.parameters: tuple[object, ...] = ()

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        self.query = query
        self.parameters = parameters

    async def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self, *, row_factory: object) -> _FakeCursor:
        assert row_factory is dict_row
        return self._cursor

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(("row", "expected"), [({"ready": True}, True), ({"ready": False}, False)])
async def test_probe_reads_only_the_exact_fresh_ready_registration(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, Any],
    expected: bool,
) -> None:
    cursor = _FakeCursor(row)
    connection = _FakeConnection(cursor)
    connect_calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_connect(database_url: str, **kwargs: Any) -> _FakeConnection:
        connect_calls.append((database_url, kwargs))
        return connection

    monkeypatch.setattr(
        "barrier_free_mobility_agent.persistence.worker_health.AsyncConnection.connect",
        fake_connect,
    )

    actual = await worker_health.probe_worker_registration_ready(
        database_url="postgresql://mobility:secret@database:5432/mobility",
        worker_id="worker-exact",
        registration_token="registration-token",
        registration_generation=3,
        timeout_seconds=1.25,
    )

    assert actual is expected
    assert connection.closed is True
    assert cursor.parameters == ("worker-exact", "registration-token", 3)
    assert "token = %s" in cursor.query
    assert "generation = %s" in cursor.query
    assert "state = 'ready'" in cursor.query
    assert "expires_at > clock_timestamp()" in cursor.query
    assert connect_calls == [
        (
            "postgresql://mobility:secret@database:5432/mobility",
            {
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
                "connect_timeout": 2,
            },
        )
    ]


def test_main_rejects_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit, match="DATABASE_URL is required"):
        worker_health.main()


@pytest.mark.parametrize("value", ["0", "nan", "not-a-number"])
def test_positive_float_env_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("PROBE_TIMEOUT", value)

    with pytest.raises(SystemExit, match="PROBE_TIMEOUT must be a positive number"):
        worker_health._positive_float_env("PROBE_TIMEOUT", default=1.0)
