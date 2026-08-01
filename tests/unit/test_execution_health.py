from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg import AsyncConnection
from pydantic import ValidationError

from barrier_free_mobility_agent.persistence.execution_health import (
    collect_execution_queue_health,
    parse_execution_queue_health,
)


def test_parse_execution_queue_health_projects_only_aggregate_fields() -> None:
    checked_at = datetime(2026, 7, 16, 8, tzinfo=UTC)
    snapshot = parse_execution_queue_health(
        {
            "checked_at": checked_at,
            "accepting_new_jobs": True,
            "ready_worker_count": 2,
            "queued_job_count": 1,
            "leased_job_count": 1,
            "expired_lease_count": 0,
            "recovery_attempt_count": 0,
            "stuck_job_count": 0,
            "oldest_queued_at": checked_at - timedelta(seconds=4),
            "oldest_queued_age_seconds": 4.0,
        },
        stuck_after=timedelta(seconds=120),
    )

    assert snapshot.ready_worker_count == 2
    assert snapshot.stuck_after_seconds == 120


def test_parse_execution_queue_health_rejects_unknown_or_inconsistent_fields() -> None:
    checked_at = datetime(2026, 7, 16, 8, tzinfo=UTC)
    with pytest.raises(ValidationError):
        parse_execution_queue_health(
            {
                "checked_at": checked_at,
                "accepting_new_jobs": False,
                "ready_worker_count": 1,
                "queued_job_count": 0,
                "leased_job_count": 0,
                "expired_lease_count": 0,
                "recovery_attempt_count": 0,
                "stuck_job_count": 0,
                "oldest_queued_at": None,
                "oldest_queued_age_seconds": None,
                "worker_id": "must-not-be-projected",
            },
            stuck_after=timedelta(seconds=120),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_url", "stuck_after", "timeout_seconds", "error"),
    [
        ("", timedelta(seconds=1), 1.0, "database_url"),
        ("postgresql://db", timedelta(0), 1.0, "stuck_after"),
        ("postgresql://db", timedelta(seconds=1), 0.0, "timeout_seconds"),
    ],
)
async def test_collect_execution_queue_health_validates_bounded_inputs(
    database_url: str,
    stuck_after: timedelta,
    timeout_seconds: float,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        await collect_execution_queue_health(
            database_url=database_url,
            stuck_after=stuck_after,
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.asyncio
async def test_collect_execution_queue_health_uses_one_bounded_read_only_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_at = datetime(2026, 7, 16, 8, tzinfo=UTC)
    observed_connect: list[tuple[str, dict[str, Any]]] = []
    observed_queries: list[tuple[str, tuple[object, ...]]] = []

    class FakeCursor:
        async def __aenter__(self) -> FakeCursor:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def execute(self, query: str, parameters: tuple[object, ...]) -> None:
            observed_queries.append((query, parameters))

        async def fetchone(self) -> dict[str, object]:
            return {
                "checked_at": checked_at,
                "accepting_new_jobs": True,
                "ready_worker_count": 2,
                "queued_job_count": 0,
                "leased_job_count": 0,
                "expired_lease_count": 0,
                "recovery_attempt_count": 0,
                "stuck_job_count": 0,
                "oldest_queued_at": None,
                "oldest_queued_age_seconds": None,
            }

    class FakeConnection:
        closed = False

        def cursor(self, **kwargs: object) -> FakeCursor:
            del kwargs
            return FakeCursor()

        async def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    async def fake_connect(database_url: str, **kwargs: Any) -> FakeConnection:
        observed_connect.append((database_url, kwargs))
        return connection

    monkeypatch.setattr(AsyncConnection, "connect", fake_connect)

    snapshot = await collect_execution_queue_health(
        database_url="postgresql://mobility:do-not-print@database:5432/mobility",
        stuck_after=timedelta(seconds=12.5),
        timeout_seconds=1.25,
    )

    assert snapshot.checked_at == checked_at
    assert snapshot.stuck_after_seconds == 12.5
    assert connection.closed is True
    assert len(observed_connect) == 1
    database_url, options = observed_connect[0]
    assert database_url.endswith("@database:5432/mobility")
    assert options["autocommit"] is True
    assert options["prepare_threshold"] == 0
    assert options["connect_timeout"] == 2
    assert options["options"] == ("-c default_transaction_read_only=on -c statement_timeout=1250")
    assert len(observed_queries) == 1
    query, parameters = observed_queries[0]
    assert "execution_worker_registry" in query
    assert "run_execution_jobs" in query
    assert parameters == (timedelta(seconds=12.5), timedelta(seconds=12.5))
