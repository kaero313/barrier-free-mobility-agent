from __future__ import annotations

import asyncio
import multiprocessing
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from tests.execution_worker_process_helper import (
    PROCESS_EXECUTION_PAYLOAD_KEY,
    run_replacement_worker,
    run_worker_until_after_claim,
)

from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.application.conversation_repository import ConversationThread
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.run_ledger import (
    RunEventType,
    RunExecutionCorrelation,
)
from barrier_free_mobility_agent.persistence.execution_payload_cipher import (
    ExecutionPayloadCipher,
)
from barrier_free_mobility_agent.persistence.postgres import (
    PostgresConversationRepository,
)
from barrier_free_mobility_agent.persistence.runtime import _configure_postgres_connection

_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests.",
)

_TERMINAL_RUN_EVENTS = {
    RunEventType.RUN_INTERRUPTED,
    RunEventType.RUN_COMPLETED,
    RunEventType.RUN_FAILED,
}


@dataclass(frozen=True, slots=True)
class _IsolatedPostgres:
    schema: str
    pool: AsyncConnectionPool[Any]
    repository: PostgresConversationRepository


@pytest.mark.asyncio
async def test_hard_killed_process_replays_safe_start_once_after_lease_expiry() -> None:
    """An OS-killed claimant is replaced only after evidence-based generation-2 replay."""

    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"
    worker_a_id = f"process-worker-a-{suffix}"
    worker_b_id = f"process-worker-b-{suffix}"
    question = f"private-process-question-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"run-{suffix}",
        request_id=f"request-{suffix}",
        attempt=1,
    )
    if _PROJECT_ROOT not in sys.path:
        # pytest's importlib mode does not add the repository root. Spawned children
        # need it to import the intentionally test-only process entry points.
        sys.path.insert(0, _PROJECT_ROOT)
    context = multiprocessing.get_context("spawn")
    after_claim = context.Event()
    replayed = context.Event()
    worker_a: BaseProcess | None = None
    worker_b: BaseProcess | None = None

    async with _isolated_postgres(_DATABASE_URL) as postgres:
        await postgres.repository.create_thread(
            ConversationThread(
                thread_id=thread_id,
                owner_id=owner_id,
                flow_version="process-chaos-flow",
                state_schema_version="process-chaos-state",
            ),
            capacity=10,
        )
        reservation = await postgres.repository.reserve_initial_execution(
            correlation,
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"idempotency-{suffix}"),
            request_fingerprint=_digest(f"request-{suffix}"),
            payload=StartExecutionPayload(question=question),
        )

        try:
            worker_a = context.Process(
                target=run_worker_until_after_claim,
                kwargs={
                    "database_url": _DATABASE_URL,
                    "schema": postgres.schema,
                    "worker_id": worker_a_id,
                    "expected_question": question,
                    "expected_thread_id": thread_id,
                    "expected_owner_id": owner_id,
                    "reached": after_claim,
                },
                name="execution-worker-after-claim-crash",
            )
            worker_a.start()
            reached_after_claim = await asyncio.to_thread(after_claim.wait, 30)
            assert reached_after_claim, (
                "worker A did not reach the deterministic AFTER_CLAIM boundary; "
                f"alive={worker_a.is_alive()} exitcode={worker_a.exitcode}"
            )
            assert worker_a.is_alive()

            generation_one = await postgres.repository.get_execution_job(
                reservation.job.job_id,
                owner_id=owner_id,
            )
            assert generation_one.state == ExecutionJobState.LEASED
            assert generation_one.claim_count == generation_one.lease_generation == 1
            assert generation_one.lease_owner == worker_a_id
            assert generation_one.lease_token is not None
            assert generation_one.checkpoint_cursor is None
            assert [
                event.event_type
                for event in await postgres.repository.get_run_events(
                    correlation.run_id,
                    owner_id=owner_id,
                )
            ] == [RunEventType.RUN_STARTED]
            assert (
                await postgres.repository.get_tool_executions(
                    correlation.run_id,
                    owner_id=owner_id,
                )
                == ()
            )
            assert await _checkpoint_count(postgres.pool) == 0

            worker_a.kill()
            await asyncio.to_thread(worker_a.join, 10)
            assert not worker_a.is_alive()
            assert worker_a.exitcode not in (None, 0)
            worker_a.close()
            worker_a = None

            worker_b = context.Process(
                target=run_replacement_worker,
                kwargs={
                    "database_url": _DATABASE_URL,
                    "schema": postgres.schema,
                    "worker_id": worker_b_id,
                    "job_id": reservation.job.job_id,
                    "expected_question": question,
                    "expected_thread_id": thread_id,
                    "expected_owner_id": owner_id,
                    "replayed": replayed,
                },
                name="execution-worker-generation-two-recovery",
            )
            worker_b.start()
            await asyncio.to_thread(worker_b.join, 45)
            if worker_b.is_alive():
                pytest.fail("worker B did not finish generation-2 recovery")
            assert worker_b.exitcode == 0
            assert replayed.is_set(), "worker B did not replay the original START operation"
            worker_b.close()
            worker_b = None

            terminal_job = await postgres.repository.get_execution_job(
                reservation.job.job_id,
                owner_id=owner_id,
            )
            assert terminal_job.state == ExecutionJobState.COMPLETED
            assert terminal_job.claim_count == terminal_job.lease_generation == 2
            assert terminal_job.payload is None
            assert terminal_job.lease_token is None
            assert terminal_job.lease_owner is None
            assert terminal_job.lease_expires_at is None
            assert terminal_job.heartbeat_at is None
            assert terminal_job.completed_at is not None
            assert terminal_job.failure_code is None
            assert terminal_job.checkpoint_cursor == "process-checkpoint:2"
            assert await _payload_storage_is_scrubbed(
                postgres.pool,
                job_id=terminal_job.job_id,
            )

            terminal_run = await postgres.repository.get_run(
                correlation.run_id,
                owner_id=owner_id,
            )
            assert terminal_run.status == GraphTurnStatus.COMPLETED
            assert terminal_run.resume_mode == ResumeMode.NONE
            events = await postgres.repository.get_run_events(
                correlation.run_id,
                owner_id=owner_id,
            )
            assert [event.event_type for event in events] == [
                RunEventType.RUN_STARTED,
                RunEventType.RUN_COMPLETED,
            ]
            assert sum(event.event_type in _TERMINAL_RUN_EVENTS for event in events) == 1
            assert (
                await postgres.repository.get_tool_executions(
                    correlation.run_id,
                    owner_id=owner_id,
                )
                == ()
            )
        finally:
            await _kill_process(worker_b)
            await _kill_process(worker_a)


async def _kill_process(process: BaseProcess | None) -> None:
    if process is None:
        return
    if process.is_alive():
        process.kill()
        await asyncio.to_thread(process.join, 5)
    process.close()


async def _checkpoint_count(pool: AsyncConnectionPool[Any]) -> int:
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute("SELECT count(*) AS checkpoint_count FROM checkpoints")
        row = await cursor.fetchone()
    assert row is not None
    return int(row["checkpoint_count"])


async def _payload_storage_is_scrubbed(
    pool: AsyncConnectionPool[Any],
    *,
    job_id: str,
) -> bool:
    async with pool.connection() as connection, connection.cursor() as cursor:
        await cursor.execute(
            """
            SELECT
                payload IS NULL
                AND payload_ciphertext IS NULL
                AND payload_nonce IS NULL
                AND payload_key_fingerprint IS NULL
                AND payload_encryption_version IS NULL AS scrubbed
            FROM run_execution_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return bool(row["scrubbed"])


@asynccontextmanager
async def _isolated_postgres(database_url: str) -> AsyncIterator[_IsolatedPostgres]:
    schema = f"execution_process_chaos_{uuid4().hex}"
    await _execute_schema_statement(database_url, "CREATE", schema)

    async def configure(connection: AsyncConnection[Any]) -> None:
        await _configure_postgres_connection(connection)
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))

    pool = AsyncConnectionPool[Any](
        conninfo=database_url,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        min_size=1,
        max_size=4,
        configure=configure,
        open=False,
        name="mobility-agent-execution-process-chaos-test",
    )
    try:
        await pool.open(wait=True, timeout=10)
        await AsyncPostgresSaver(pool).setup()
        repository = PostgresConversationRepository(
            pool,
            execution_payload_cipher=ExecutionPayloadCipher(PROCESS_EXECUTION_PAYLOAD_KEY),
        )
        await repository.setup()
        yield _IsolatedPostgres(schema=schema, pool=pool, repository=repository)
    finally:
        try:
            await pool.close()
        finally:
            await _execute_schema_statement(database_url, "DROP", schema)


async def _execute_schema_statement(
    database_url: str,
    operation: str,
    schema: str,
) -> None:
    connection = await AsyncConnection.connect(database_url, autocommit=True)
    try:
        if operation == "CREATE":
            statement = sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        elif operation == "DROP":
            statement = sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        else:  # pragma: no cover - private test helper contract
            raise ValueError("unsupported schema operation")
        await connection.execute(statement)
    finally:
        await connection.close()


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
