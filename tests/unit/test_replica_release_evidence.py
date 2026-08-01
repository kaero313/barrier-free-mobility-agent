from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.rows import dict_row
from pydantic import ValidationError

import barrier_free_mobility_agent.persistence.replica_release_evidence as release_evidence
from barrier_free_mobility_agent.persistence.replica_release_evidence import (
    ReplicaReleaseDatabaseEvidence,
    collect_replica_release_database_evidence,
    parse_replica_release_database_evidence,
)


def _evidence_row(**overrides: object) -> dict[str, object]:
    checked_at = datetime(2026, 7, 16, 6, 0, tzinfo=UTC)
    row: dict[str, object] = {
        "checked_at": checked_at,
        "window_started_at": checked_at - timedelta(hours=1),
        "ready_worker_count": 2,
        "registered_worker_count": 2,
        "expired_registration_count": 0,
        "draining_worker_count": 0,
        "registration_generation_total": 5,
        "created_job_count": 8,
        "completed_job_count": 5,
        "failed_job_count": 1,
        "active_job_count": 2,
        "recovered_job_count": 2,
        "recovery_attempt_count": 3,
        "max_claim_count": 3,
        "ambiguous_recovery_count": 1,
        "terminal_payload_retention_count": 0,
        "terminal_event_violation_count": 0,
        "active_lease_without_ready_worker_count": 1,
    }
    row.update(overrides)
    return row


def test_parse_validates_and_freezes_low_cardinality_evidence() -> None:
    evidence = parse_replica_release_database_evidence(_evidence_row())

    assert evidence.schema_version == "1"
    assert evidence.created_job_count == 8
    assert evidence.recovery_attempt_count == 3
    assert evidence.model_dump() == {"schema_version": "1", **_evidence_row()}

    field_name = "ready_worker_count"
    with pytest.raises(ValidationError, match="Instance is frozen"):
        setattr(evidence, field_name, 1)


def test_parse_rejects_identifier_or_payload_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_replica_release_database_evidence(
            _evidence_row(run_id="run-must-not-enter-evidence", payload={"question": "private"})
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"window_started_at": datetime(2026, 7, 16, 7, 0, tzinfo=UTC)}, "cannot follow"),
        ({"ready_worker_count": 3}, "cannot exceed registered"),
        ({"registration_generation_total": 1}, "positive generation"),
        ({"active_job_count": 1}, "state counts"),
        ({"recovered_job_count": 4, "recovery_attempt_count": 3}, "recovery attempt"),
        ({"ambiguous_recovery_count": 2}, "cannot exceed failed"),
        ({"terminal_payload_retention_count": 7}, "cannot exceed terminal"),
        ({"terminal_event_violation_count": 9}, "cannot exceed created"),
        ({"active_lease_without_ready_worker_count": 3}, "cannot exceed active"),
    ],
)
def test_parse_rejects_inconsistent_aggregate_rows(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_replica_release_database_evidence(_evidence_row(**overrides))


def test_model_and_final_projection_forbid_sensitive_release_data() -> None:
    forbidden_fields = {
        "job_id",
        "owner_id",
        "thread_id",
        "run_id",
        "request_id",
        "worker_id",
        "token",
        "lease_token",
        "lease_owner",
        "payload",
        "payload_ciphertext",
        "payload_nonce",
        "payload_key_fingerprint",
        "question",
    }
    assert forbidden_fields.isdisjoint(ReplicaReleaseDatabaseEvidence.model_fields)

    normalized_query = " ".join(release_evidence._REPLICA_RELEASE_EVIDENCE_SQL.lower().split())
    final_projection = normalized_query.rsplit(" select ", maxsplit=1)[1].split(
        " from execution_clock",
        maxsplit=1,
    )[0]
    assert "*" not in final_projection
    assert forbidden_fields.isdisjoint(final_projection.replace(",", " ").split())
    assert "worker.token" not in normalized_query
    assert "job.lease_token" not in normalized_query
    assert "job.owner_id" not in normalized_query
    assert "job.thread_id" not in normalized_query
    assert "job.job_id" not in normalized_query
    assert "terminal_event.payload" not in normalized_query


class _FakeCursor:
    def __init__(self, row: dict[str, Any] | None, *, error: Exception | None = None) -> None:
        self._row = row
        self._error = error
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        self.execute_calls.append((query, parameters))
        if self._error is not None:
            raise self._error

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
async def test_collect_uses_one_bounded_read_only_statement_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window_started_at = datetime(2026, 7, 16, 5, 0, tzinfo=UTC)
    cursor = _FakeCursor(_evidence_row(window_started_at=window_started_at))
    connection = _FakeConnection(cursor)
    connect_calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_connect(database_url: str, **kwargs: Any) -> _FakeConnection:
        connect_calls.append((database_url, kwargs))
        return connection

    monkeypatch.setattr(
        "barrier_free_mobility_agent.persistence.replica_release_evidence.AsyncConnection.connect",
        fake_connect,
    )

    evidence = await collect_replica_release_database_evidence(
        database_url="postgresql://mobility:secret@database:5432/mobility",
        window_started_at=window_started_at,
        timeout_seconds=1.25,
    )

    assert evidence.window_started_at == window_started_at
    assert connection.closed is True
    assert cursor.execute_calls == [
        (release_evidence._REPLICA_RELEASE_EVIDENCE_SQL, (window_started_at,))
    ]
    assert connect_calls == [
        (
            "postgresql://mobility:secret@database:5432/mobility",
            {
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
                "connect_timeout": 2,
                "options": ("-c default_transaction_read_only=on -c statement_timeout=1250"),
            },
        )
    ]


@pytest.mark.asyncio
async def test_collect_closes_connection_when_the_query_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(_FakeCursor(None, error=RuntimeError("query failed")))

    async def fake_connect(database_url: str, **kwargs: Any) -> _FakeConnection:
        del database_url, kwargs
        return connection

    monkeypatch.setattr(
        "barrier_free_mobility_agent.persistence.replica_release_evidence.AsyncConnection.connect",
        fake_connect,
    )

    with pytest.raises(RuntimeError, match="query failed"):
        await collect_replica_release_database_evidence(
            database_url="postgresql://database/release",
            window_started_at=datetime(2026, 7, 16, tzinfo=UTC),
            timeout_seconds=1,
        )

    assert connection.closed is True


@pytest.mark.parametrize("timeout_seconds", [0, -1, float("nan"), float("inf")])
@pytest.mark.asyncio
async def test_collect_rejects_unbounded_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        await collect_replica_release_database_evidence(
            database_url="postgresql://database/release",
            window_started_at=datetime(2026, 7, 16, tzinfo=UTC),
            timeout_seconds=timeout_seconds,
        )
