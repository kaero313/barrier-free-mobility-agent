from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionQueueHealth,
)

_EXECUTION_QUEUE_HEALTH_SQL = """
WITH execution_clock AS MATERIALIZED (
    SELECT clock_timestamp() AS effective_now
),
worker_health AS MATERIALIZED (
    SELECT count(*) FILTER (
        WHERE worker.state = 'ready'
          AND worker.expires_at > execution_clock.effective_now
    ) AS ready_worker_count
    FROM execution_worker_registry AS worker
    CROSS JOIN execution_clock
),
job_health AS MATERIALIZED (
    SELECT
        count(*) FILTER (WHERE job.state = 'queued') AS queued_job_count,
        count(*) FILTER (WHERE job.state = 'leased') AS leased_job_count,
        count(*) FILTER (
            WHERE job.state = 'leased'
              AND job.lease_expires_at <= execution_clock.effective_now
        ) AS expired_lease_count,
        COALESCE(
            sum(GREATEST(job.lease_generation - 1, 0)) FILTER (
                WHERE job.state IN ('queued', 'leased')
            ),
            0
        ) AS recovery_attempt_count,
        count(*) FILTER (
            WHERE (
                job.state = 'queued'
                AND job.available_at <= execution_clock.effective_now - %s::interval
            ) OR (
                job.state = 'leased'
                AND job.lease_expires_at <= execution_clock.effective_now - %s::interval
            )
        ) AS stuck_job_count,
        min(job.created_at) FILTER (
            WHERE job.state = 'queued'
        ) AS oldest_queued_at
    FROM run_execution_jobs AS job
    CROSS JOIN execution_clock
    WHERE job.state IN ('queued', 'leased')
)
SELECT
    execution_clock.effective_now AS checked_at,
    worker_health.ready_worker_count > 0 AS accepting_new_jobs,
    worker_health.ready_worker_count,
    job_health.queued_job_count,
    job_health.leased_job_count,
    job_health.expired_lease_count,
    job_health.recovery_attempt_count,
    job_health.stuck_job_count,
    job_health.oldest_queued_at,
    CASE
        WHEN job_health.oldest_queued_at IS NULL THEN NULL
        ELSE GREATEST(
            0,
            EXTRACT(
                EPOCH FROM (
                    execution_clock.effective_now - job_health.oldest_queued_at
                )
            )
        )::double precision
    END AS oldest_queued_age_seconds
FROM execution_clock, worker_health, job_health
"""


def parse_execution_queue_health(
    row: Mapping[str, object],
    *,
    stuck_after: timedelta,
) -> ExecutionQueueHealth:
    """Validate one aggregate row without retaining job or worker identifiers."""

    return ExecutionQueueHealth.model_validate(
        {
            **dict(row),
            "stuck_after_seconds": stuck_after.total_seconds(),
        }
    )


async def collect_execution_queue_health(
    *,
    database_url: str,
    stuck_after: timedelta,
    timeout_seconds: float,
) -> ExecutionQueueHealth:
    """Read queue health through one bounded connection without booting persistence."""

    if not database_url.strip():
        raise ValueError("database_url must not be blank")
    if not math.isfinite(stuck_after.total_seconds()) or stuck_after <= timedelta(0):
        raise ValueError("stuck_after must be a positive finite duration")
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
                    _EXECUTION_QUEUE_HEALTH_SQL,
                    (stuck_after, stuck_after),
                )
                row = await cursor.fetchone()
    finally:
        if connection is not None:
            await connection.close()

    if row is None:  # pragma: no cover - aggregate query always returns one row
        raise RuntimeError("execution queue health query did not return a row")
    return parse_execution_queue_health(row, stuck_after=stuck_after)
