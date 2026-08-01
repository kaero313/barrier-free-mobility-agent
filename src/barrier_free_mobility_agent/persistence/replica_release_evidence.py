from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Self

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReplicaReleaseDatabaseEvidence(BaseModel):
    """Redacted, low-cardinality database evidence for one replica release window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    checked_at: datetime
    window_started_at: datetime
    ready_worker_count: int = Field(ge=0)
    registered_worker_count: int = Field(ge=0)
    expired_registration_count: int = Field(ge=0)
    draining_worker_count: int = Field(ge=0)
    registration_generation_total: int = Field(ge=0)
    created_job_count: int = Field(ge=0)
    completed_job_count: int = Field(ge=0)
    failed_job_count: int = Field(ge=0)
    active_job_count: int = Field(ge=0)
    recovered_job_count: int = Field(ge=0)
    recovery_attempt_count: int = Field(ge=0)
    max_claim_count: int = Field(ge=0)
    ambiguous_recovery_count: int = Field(ge=0)
    terminal_payload_retention_count: int = Field(ge=0)
    terminal_event_violation_count: int = Field(ge=0)
    active_lease_without_ready_worker_count: int = Field(ge=0)

    @field_validator("checked_at", "window_started_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("replica release evidence timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.window_started_at > self.checked_at:
            raise ValueError("window_started_at cannot follow checked_at")
        if self.ready_worker_count > self.registered_worker_count:
            raise ValueError("ready workers cannot exceed registered workers")
        if self.expired_registration_count > self.registered_worker_count:
            raise ValueError("expired registrations cannot exceed registered workers")
        if self.draining_worker_count > self.registered_worker_count:
            raise ValueError("draining workers cannot exceed registered workers")
        if self.ready_worker_count + self.expired_registration_count > (
            self.registered_worker_count
        ):
            raise ValueError("fresh ready and expired registrations must be disjoint")
        if self.ready_worker_count + self.draining_worker_count > self.registered_worker_count:
            raise ValueError("ready and draining registrations must be disjoint")
        if self.registered_worker_count == 0:
            if self.registration_generation_total != 0:
                raise ValueError("an empty registry cannot have generations")
        elif self.registration_generation_total < self.registered_worker_count:
            raise ValueError("each registered worker must have a positive generation")

        terminal_job_count = self.completed_job_count + self.failed_job_count
        if terminal_job_count + self.active_job_count != self.created_job_count:
            raise ValueError("window job state counts must equal created_job_count")
        if self.recovered_job_count > self.created_job_count:
            raise ValueError("recovered jobs cannot exceed created jobs")
        if self.recovered_job_count > self.recovery_attempt_count:
            raise ValueError("each recovered job must contribute a recovery attempt")
        if self.created_job_count == 0 and self.max_claim_count != 0:
            raise ValueError("an empty job window cannot have a claim count")
        if self.recovered_job_count > 0 and self.max_claim_count < 2:
            raise ValueError("a recovered job must have at least two claims")
        if self.ambiguous_recovery_count > self.failed_job_count:
            raise ValueError("ambiguous recoveries cannot exceed failed jobs")
        if self.terminal_payload_retention_count > terminal_job_count:
            raise ValueError("terminal payload retention cannot exceed terminal jobs")
        if self.terminal_event_violation_count > self.created_job_count:
            raise ValueError("terminal event violations cannot exceed created jobs")
        if self.active_lease_without_ready_worker_count > self.active_job_count:
            raise ValueError("unowned active leases cannot exceed active jobs")
        return self


_REPLICA_RELEASE_EVIDENCE_SQL = """
WITH execution_clock AS MATERIALIZED (
    SELECT
        clock_timestamp() AS checked_at,
        %s::timestamptz AS window_started_at
),
worker_evidence AS MATERIALIZED (
    SELECT
        count(*) FILTER (
            WHERE worker.state = 'ready'
              AND worker.expires_at > execution_clock.checked_at
        ) AS ready_worker_count,
        count(*) AS registered_worker_count,
        count(*) FILTER (
            WHERE worker.expires_at <= execution_clock.checked_at
        ) AS expired_registration_count,
        count(*) FILTER (
            WHERE worker.state = 'draining'
        ) AS draining_worker_count,
        COALESCE(sum(worker.generation), 0)::bigint AS registration_generation_total
    FROM execution_worker_registry AS worker
    CROSS JOIN execution_clock
),
window_job_evidence AS MATERIALIZED (
    SELECT
        job.state,
        job.claim_count,
        job.failure_code = 'WORKER_RECOVERY_AMBIGUOUS' AS ambiguous_recovery,
        job.state IN ('completed', 'failed')
            AND (
                job.payload IS NOT NULL
                OR job.payload_ciphertext IS NOT NULL
                OR job.payload_nonce IS NOT NULL
                OR job.payload_key_fingerprint IS NOT NULL
                OR job.payload_encryption_version IS NOT NULL
            ) AS terminal_payload_retained,
        (
            SELECT count(*)
            FROM run_events AS terminal_event
            WHERE terminal_event.run_id = job.run_id
              AND terminal_event.request_id = job.request_id
              AND terminal_event.event_type IN (
                  'run.interrupted',
                  'run.completed',
                  'run.failed'
              )
        ) AS terminal_event_count,
        job.state = 'leased'
            AND job.lease_expires_at > execution_clock.checked_at
            AND NOT EXISTS (
                SELECT 1
                FROM execution_worker_registry AS lease_worker
                WHERE lease_worker.worker_id = job.lease_owner
                  AND lease_worker.state = 'ready'
                  AND lease_worker.expires_at > execution_clock.checked_at
            ) AS active_lease_without_ready_worker
    FROM run_execution_jobs AS job
    CROSS JOIN execution_clock
    WHERE job.created_at >= execution_clock.window_started_at
      AND job.created_at <= execution_clock.checked_at
),
job_evidence AS MATERIALIZED (
    SELECT
        count(*) AS created_job_count,
        count(*) FILTER (WHERE state = 'completed') AS completed_job_count,
        count(*) FILTER (WHERE state = 'failed') AS failed_job_count,
        count(*) FILTER (WHERE state IN ('queued', 'leased')) AS active_job_count,
        count(*) FILTER (WHERE claim_count > 1) AS recovered_job_count,
        COALESCE(sum(GREATEST(claim_count - 1, 0)), 0)::bigint
            AS recovery_attempt_count,
        COALESCE(max(claim_count), 0) AS max_claim_count,
        count(*) FILTER (WHERE ambiguous_recovery) AS ambiguous_recovery_count,
        count(*) FILTER (WHERE terminal_payload_retained)
            AS terminal_payload_retention_count,
        count(*) FILTER (
            WHERE (
                state IN ('completed', 'failed')
                AND terminal_event_count <> 1
            ) OR (
                state IN ('queued', 'leased')
                AND terminal_event_count <> 0
            )
        ) AS terminal_event_violation_count,
        count(*) FILTER (WHERE active_lease_without_ready_worker)
            AS active_lease_without_ready_worker_count
    FROM window_job_evidence
)
SELECT
    execution_clock.checked_at,
    execution_clock.window_started_at,
    worker_evidence.ready_worker_count,
    worker_evidence.registered_worker_count,
    worker_evidence.expired_registration_count,
    worker_evidence.draining_worker_count,
    worker_evidence.registration_generation_total,
    job_evidence.created_job_count,
    job_evidence.completed_job_count,
    job_evidence.failed_job_count,
    job_evidence.active_job_count,
    job_evidence.recovered_job_count,
    job_evidence.recovery_attempt_count,
    job_evidence.max_claim_count,
    job_evidence.ambiguous_recovery_count,
    job_evidence.terminal_payload_retention_count,
    job_evidence.terminal_event_violation_count,
    job_evidence.active_lease_without_ready_worker_count
FROM execution_clock, worker_evidence, job_evidence
"""


def parse_replica_release_database_evidence(
    row: Mapping[str, object],
) -> ReplicaReleaseDatabaseEvidence:
    """Validate one aggregate query row without retaining any source identifiers."""

    return ReplicaReleaseDatabaseEvidence.model_validate(dict(row))


async def collect_replica_release_database_evidence(
    *,
    database_url: str,
    window_started_at: datetime,
    timeout_seconds: float,
) -> ReplicaReleaseDatabaseEvidence:
    """Collect one DB-clock snapshot through a bounded, read-only PostgreSQL session."""

    if not database_url.strip():
        raise ValueError("database_url must not be blank")
    if window_started_at.tzinfo is None or window_started_at.utcoffset() is None:
        raise ValueError("window_started_at must be timezone-aware")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")

    connection: AsyncConnection[dict[str, Any]] | None = None
    row: dict[str, Any] | None = None
    statement_timeout_ms = max(1, math.ceil(timeout_seconds * 1000))
    try:
        async with asyncio.timeout(timeout_seconds):
            connection = await AsyncConnection.connect(
                database_url,
                autocommit=True,
                prepare_threshold=0,
                row_factory=dict_row,
                connect_timeout=max(1, math.ceil(timeout_seconds)),
                options=(
                    "-c default_transaction_read_only=on "
                    f"-c statement_timeout={statement_timeout_ms}"
                ),
            )
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    _REPLICA_RELEASE_EVIDENCE_SQL,
                    (window_started_at,),
                )
                row = await cursor.fetchone()
    finally:
        if connection is not None:
            await connection.close()

    if row is None:  # pragma: no cover - aggregate query always returns one row
        raise RuntimeError("replica release evidence query did not return a row")
    return parse_replica_release_database_evidence(row)
