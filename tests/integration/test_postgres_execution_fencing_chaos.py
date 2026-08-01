from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.application.conversation_repository import ConversationThread
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.domain.errors import ExecutionLeaseLostError
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    ExecutionLease,
    RunExecutionJob,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.models import AnswerOutcome
from barrier_free_mobility_agent.domain.routing import ToolCapability
from barrier_free_mobility_agent.domain.run_ledger import (
    ResolveStationToolSummary,
    RunEventRecord,
    RunEventType,
    RunExecutionCorrelation,
    ToolExecutionRecord,
    ToolTransition,
)
from barrier_free_mobility_agent.persistence.execution_payload_cipher import (
    ExecutionPayloadCipher,
)
from barrier_free_mobility_agent.persistence.postgres import (
    PostgresConversationRepository,
)
from barrier_free_mobility_agent.persistence.runtime import _configure_postgres_connection

_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests.",
)


@dataclass(frozen=True)
class _DurableExecutionSnapshot:
    job: RunExecutionJob
    events: tuple[RunEventRecord, ...]
    tool_executions: tuple[ToolExecutionRecord, ...]


@pytest.mark.asyncio
async def test_postgres_reclaim_fences_every_stale_generation_mutation() -> None:
    """A reclaimed lease is the only writer across queue, ledger, and Tool state."""

    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"run-{suffix}",
        request_id=f"request-{suffix}",
        attempt=1,
    )
    sentinel = f"private-question-{suffix}"

    async with _isolated_repository(_DATABASE_URL) as repository:
        await repository.create_thread(
            _conversation_thread(thread_id, owner_id),
            capacity=10,
        )
        reservation = await repository.reserve_initial_execution(
            correlation,
            owner_id=owner_id,
            capacity=10,
            idempotency_digest=_digest(f"idempotency-{suffix}"),
            request_fingerprint=_digest(f"request-{suffix}"),
            payload=StartExecutionPayload(question=sentinel),
        )
        claim_at = reservation.job.available_at + timedelta(milliseconds=1)
        worker_a = await repository.register_execution_worker(
            f"worker-a-{suffix}",
            heartbeat_ttl=timedelta(hours=1),
            now=claim_at,
        )
        worker_b = await repository.register_execution_worker(
            f"worker-b-{suffix}",
            heartbeat_ttl=timedelta(hours=1),
            now=claim_at,
        )
        assert worker_a.worker_id != worker_b.worker_id
        assert worker_a.token != worker_b.token

        claims = await asyncio.gather(
            repository.claim_execution_job(
                registration=worker_a,
                lease_duration=timedelta(seconds=10),
                now=claim_at,
            ),
            repository.claim_execution_job(
                registration=worker_b,
                lease_duration=timedelta(seconds=10),
                now=claim_at,
            ),
        )
        claimed = [lease for lease in claims if lease is not None]
        assert len(claimed) == 1
        assert sum(lease is None for lease in claims) == 1
        generation_one = claimed[0]
        assert generation_one.generation == 1
        assert generation_one.worker_id in {worker_a.worker_id, worker_b.worker_id}

        first_tool = await repository.append_tool_started(
            correlation,
            owner_id=owner_id,
            execution_id=f"tool-first-{suffix}",
            logical_call_id=f"logical-first-{suffix}",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
            execution_lease=generation_one,
        )

        reclaimer = worker_b if generation_one.worker_id == worker_a.worker_id else worker_a
        generation_two = await repository.claim_execution_job(
            registration=reclaimer,
            lease_duration=timedelta(minutes=2),
            now=generation_one.expires_at,
        )
        assert generation_two is not None
        assert generation_two.job_id == generation_one.job_id
        assert generation_two.worker_id == reclaimer.worker_id
        assert generation_two.generation == 2
        assert generation_two.token != generation_one.token

        snapshot = await _snapshot_execution(
            repository,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        assert snapshot.job.state == ExecutionJobState.LEASED
        assert snapshot.job.lease_generation == snapshot.job.claim_count == 2
        assert snapshot.job.lease_token == generation_two.token
        assert snapshot.job.lease_owner == generation_two.worker_id
        assert [event.event_type for event in snapshot.events] == [
            RunEventType.RUN_STARTED,
            RunEventType.TOOL_STARTED,
        ]
        assert snapshot.tool_executions == ()

        stale_at = generation_two.heartbeat_at + timedelta(milliseconds=1)
        terminal_run = ConversationRun(
            run_id=correlation.run_id,
            thread_id=thread_id,
            status=GraphTurnStatus.COMPLETED,
            resume_mode=ResumeMode.NONE,
            outcome=AnswerOutcome.question_too_complex(),
        )
        tool_summary = ResolveStationToolSummary(
            matched=True,
            candidate_count=1,
            clarification_required=False,
        )

        await _assert_stale_write_rejected_without_change(
            repository.assert_execution_lease(generation_one, now=stale_at),
            repository=repository,
            expected=snapshot,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        await _assert_stale_write_rejected_without_change(
            repository.heartbeat_execution_job(
                generation_one,
                lease_duration=timedelta(minutes=1),
                checkpoint_cursor=f"stale-heartbeat-{suffix}",
                now=stale_at,
            ),
            repository=repository,
            expected=snapshot,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        await _assert_stale_write_rejected_without_change(
            repository.release_execution_job(
                generation_one,
                available_at=stale_at,
                checkpoint_cursor=f"stale-release-{suffix}",
                now=stale_at,
            ),
            repository=repository,
            expected=snapshot,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        await _assert_stale_write_rejected_without_change(
            repository.fail_execution_job(
                generation_one,
                failure_code="STALE_WORKER_FAILURE",
                checkpoint_cursor=f"stale-fail-{suffix}",
                now=stale_at,
            ),
            repository=repository,
            expected=snapshot,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        await _assert_stale_write_rejected_without_change(
            repository.complete_execution_job(
                generation_one,
                checkpoint_cursor=f"stale-complete-{suffix}",
                now=stale_at,
            ),
            repository=repository,
            expected=snapshot,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        await _assert_stale_write_rejected_without_change(
            repository.finalize_execution(
                terminal_run,
                correlation=correlation,
                owner_id=owner_id,
                execution_lease=generation_one,
                job_state=ExecutionJobState.COMPLETED,
                checkpoint_cursor=f"stale-finalize-{suffix}",
            ),
            repository=repository,
            expected=snapshot,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        await _assert_stale_write_rejected_without_change(
            repository.append_tool_started(
                correlation,
                owner_id=owner_id,
                execution_id=f"tool-stale-{suffix}",
                logical_call_id=f"logical-stale-{suffix}",
                attempt=1,
                tool=ToolCapability.RESOLVE_STATION,
                execution_lease=generation_one,
            ),
            repository=repository,
            expected=snapshot,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        await _assert_stale_write_rejected_without_change(
            repository.append_tool_terminal(
                first_tool,
                owner_id=owner_id,
                transition=ToolTransition.COMPLETED,
                duration_ms=1,
                summary=tool_summary,
                execution_lease=generation_one,
            ),
            repository=repository,
            expected=snapshot,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        await _assert_stale_write_rejected_without_change(
            _enter_stale_lease_guard(repository, generation_one),
            repository=repository,
            expected=snapshot,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )

        await repository.append_tool_terminal(
            first_tool,
            owner_id=owner_id,
            transition=ToolTransition.COMPLETED,
            duration_ms=1,
            summary=tool_summary,
            execution_lease=generation_two,
        )
        assert (
            await repository.finalize_execution(
                terminal_run,
                correlation=correlation,
                owner_id=owner_id,
                execution_lease=generation_two,
                job_state=ExecutionJobState.COMPLETED,
                checkpoint_cursor=f"generation-two-terminal-{suffix}",
            )
            == terminal_run
        )

        terminal = await _snapshot_execution(
            repository,
            job_id=generation_two.job_id,
            run_id=correlation.run_id,
            owner_id=owner_id,
        )
        assert terminal.job.state == ExecutionJobState.COMPLETED
        assert terminal.job.payload is None
        assert terminal.job.lease_generation == terminal.job.claim_count == 2
        assert terminal.job.lease_token is None
        assert terminal.job.lease_owner is None
        assert terminal.job.failure_code is None
        assert terminal.job.checkpoint_cursor == f"generation-two-terminal-{suffix}"
        assert [event.event_type for event in terminal.events] == [
            RunEventType.RUN_STARTED,
            RunEventType.TOOL_STARTED,
            RunEventType.TOOL_COMPLETED,
            RunEventType.RUN_COMPLETED,
        ]
        assert len(terminal.tool_executions) == 1
        assert terminal.tool_executions[0].handle == first_tool


async def _assert_stale_write_rejected_without_change(
    operation: Awaitable[object],
    *,
    repository: PostgresConversationRepository,
    expected: _DurableExecutionSnapshot,
    job_id: str,
    run_id: str,
    owner_id: str,
) -> None:
    with pytest.raises(ExecutionLeaseLostError):
        await operation
    assert (
        await _snapshot_execution(
            repository,
            job_id=job_id,
            run_id=run_id,
            owner_id=owner_id,
        )
        == expected
    )


async def _enter_stale_lease_guard(
    repository: PostgresConversationRepository,
    lease: ExecutionLease,
) -> None:
    async with repository.guard_execution_lease(lease):
        raise AssertionError("a stale lease entered its guarded write section")


async def _snapshot_execution(
    repository: PostgresConversationRepository,
    *,
    job_id: str,
    run_id: str,
    owner_id: str,
) -> _DurableExecutionSnapshot:
    return _DurableExecutionSnapshot(
        job=await repository.get_execution_job(job_id, owner_id=owner_id),
        events=await repository.get_run_events(run_id, owner_id=owner_id),
        tool_executions=await repository.get_tool_executions(run_id, owner_id=owner_id),
    )


@asynccontextmanager
async def _isolated_repository(
    database_url: str,
) -> AsyncIterator[PostgresConversationRepository]:
    schema = f"execution_fencing_{uuid4().hex}"
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
        name="mobility-agent-execution-fencing-chaos-test",
    )
    try:
        await pool.open(wait=True, timeout=10)
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        repository = PostgresConversationRepository(
            pool,
            allow_test_clock=True,
            execution_payload_cipher=ExecutionPayloadCipher(b"f" * 32),
        )
        await repository.setup()
        yield repository
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


def _conversation_thread(thread_id: str, owner_id: str) -> ConversationThread:
    return ConversationThread(
        thread_id=thread_id,
        owner_id=owner_id,
        flow_version="test-flow",
        state_schema_version="test-state",
    )


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
