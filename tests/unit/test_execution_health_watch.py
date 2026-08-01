from __future__ import annotations

import io
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from pydantic import ValidationError

import barrier_free_mobility_agent.persistence.execution_health_watch as health_watch
from barrier_free_mobility_agent.domain.execution_queue import ExecutionQueueHealth


def _snapshot() -> ExecutionQueueHealth:
    return ExecutionQueueHealth(
        checked_at=datetime(2026, 7, 16, 9, tzinfo=UTC),
        accepting_new_jobs=True,
        ready_worker_count=2,
        queued_job_count=0,
        leased_job_count=0,
        expired_lease_count=0,
        recovery_attempt_count=0,
        stuck_job_count=0,
        oldest_queued_at=None,
        oldest_queued_age_seconds=None,
        stuck_after_seconds=120,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transient_error",
    [
        OperationalError("private connection failed"),
        PoolTimeout("private pool timed out"),
        TimeoutError("private query timed out"),
        OSError("private socket failed"),
    ],
)
async def test_watch_recovers_after_transient_database_error_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
    transient_error: Exception,
) -> None:
    secret = "postgresql://private-user:private-password@database/private"
    stop_event = threading.Event()
    emitted: list[str] = []
    calls = 0

    async def collect(**_: object) -> ExecutionQueueHealth:
        nonlocal calls
        calls += 1
        if calls == 1:
            transient_error.add_note(secret)
            raise transient_error
        return _snapshot()

    def emit(payload: str) -> None:
        emitted.append(payload)
        if len(emitted) == 2:
            stop_event.set()

    monkeypatch.setattr(health_watch, "collect_execution_queue_health", collect)

    await health_watch.run_execution_health_watch(
        database_url=secret,
        stuck_after=timedelta(seconds=120),
        interval_seconds=0.1,
        timeout_seconds=1,
        stop_event=stop_event,
        emit=emit,
    )

    assert json.loads(emitted[0]) == {
        "schema_version": "1",
        "event": "unavailable",
        "error_code": "execution_health_unavailable",
    }
    assert secret not in "".join(emitted)
    recovered = health_watch.parse_execution_health_watch_event(emitted[1])
    assert isinstance(recovered, health_watch.ExecutionHealthSnapshotEvent)
    assert recovered.snapshot.ready_worker_count == 2
    assert {
        "worker_id",
        "job_id",
        "run_id",
        "thread_id",
        "owner_id",
        "token",
        "payload",
    }.isdisjoint(json.loads(emitted[1])["snapshot"])


def test_watch_protocol_is_closed_and_rejects_identifier_fields() -> None:
    payload = health_watch.ExecutionHealthSnapshotEvent(snapshot=_snapshot()).model_dump_json()
    document = json.loads(payload)
    document["snapshot"]["worker_id"] = "private-worker"

    with pytest.raises(ValidationError):
        health_watch.parse_execution_health_watch_event(json.dumps(document))


def test_watch_protocol_rejects_coerced_numeric_snapshot_fields() -> None:
    document = json.loads(
        health_watch.ExecutionHealthSnapshotEvent(snapshot=_snapshot()).model_dump_json()
    )
    document["snapshot"]["ready_worker_count"] = "2"

    with pytest.raises(ValidationError):
        health_watch.parse_execution_health_watch_event(json.dumps(document))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stuck_after", timedelta(0)),
        ("stuck_after", timedelta(days=2)),
        ("interval_seconds", 0.0),
        ("interval_seconds", 61.0),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", 31.0),
    ],
)
def test_watch_rejects_unbounded_runtime_inputs(field: str, value: object) -> None:
    arguments: dict[str, object] = {
        "database_url": "postgresql://database/mobility",
        "stuck_after": timedelta(seconds=120),
        "interval_seconds": 2.0,
        "timeout_seconds": 2.0,
    }
    arguments[field] = value

    with pytest.raises(ValueError, match="must be between"):
        health_watch._validate_runtime_inputs(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [b"\n", b"anything\n", b""])
def test_stdin_newline_or_eof_requests_clean_stop(payload: bytes) -> None:
    stop_event = threading.Event()

    health_watch._request_stop_on_stdin(io.BytesIO(payload), stop_event)

    assert stop_event.is_set()
