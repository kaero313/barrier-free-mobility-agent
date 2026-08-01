from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Protocol

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from barrier_free_mobility_agent.agent.models import GraphTurn, GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.application.execution_checkpoint import (
    ExecutionCheckpointInspection,
    ExecutionCheckpointState,
)
from barrier_free_mobility_agent.application.execution_worker import (
    DurableExecutionWorker,
    ExecutionWorkerFaultPoint,
    ExecutionWorkerOptions,
)
from barrier_free_mobility_agent.domain.clarification import FieldsClarificationAnswer
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    ExecutionLease,
)
from barrier_free_mobility_agent.domain.identity import OwnerId
from barrier_free_mobility_agent.domain.models import AnswerOutcome
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation
from barrier_free_mobility_agent.persistence.execution_payload_cipher import (
    ExecutionPayloadCipher,
)
from barrier_free_mobility_agent.persistence.postgres import (
    PostgresConversationRepository,
)
from barrier_free_mobility_agent.persistence.runtime import _configure_postgres_connection

PROCESS_EXECUTION_PAYLOAD_KEY = b"p" * 32


class ProcessSignal(Protocol):
    def set(self) -> None: ...


class _BlockAfterClaim:
    def __init__(self, reached: ProcessSignal) -> None:
        self._reached = reached

    async def hit(
        self,
        point: ExecutionWorkerFaultPoint,
        lease: ExecutionLease,
    ) -> None:
        del lease
        if point != ExecutionWorkerFaultPoint.AFTER_CLAIM:
            return
        self._reached.set()
        await asyncio.Event().wait()


class _ReplayStartGraph:
    def __init__(
        self,
        *,
        expected_question: str,
        expected_thread_id: str,
        expected_owner_id: str,
        replayed: ProcessSignal,
    ) -> None:
        self._expected_question = expected_question
        self._expected_thread_id = expected_thread_id
        self._expected_owner_id = expected_owner_id
        self._replayed = replayed
        self._inspection = ExecutionCheckpointInspection(state=ExecutionCheckpointState.MISSING)

    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
    ) -> GraphTurn:
        if question != self._expected_question:
            raise AssertionError("replacement worker did not receive the original START input")
        if thread_id != self._expected_thread_id or correlation.thread_id != thread_id:
            raise AssertionError("replacement worker replayed START for the wrong thread")
        if owner_id != self._expected_owner_id:
            raise AssertionError("replacement worker replayed START for the wrong owner")
        self._replayed.set()
        self._inspection = ExecutionCheckpointInspection(
            state=ExecutionCheckpointState.TERMINAL,
            cursor=f"process-checkpoint:{execution_lease.generation}",
            correlation=correlation,
            execution_job_id=execution_lease.job_id,
            lease_generation=execution_lease.generation,
        )
        return GraphTurn(
            status=GraphTurnStatus.COMPLETED,
            resume_mode=ResumeMode.NONE,
            outcome=AnswerOutcome.question_too_complex(),
            state={},
        )

    async def resume(
        self,
        answer: str | FieldsClarificationAnswer,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
    ) -> GraphTurn:
        del answer, thread_id, owner_id, correlation, execution_lease
        raise AssertionError("a reclaimed START must not enter command resume")

    async def continue_business_clarification(
        self,
        answer: str,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
    ) -> GraphTurn:
        del answer, thread_id, owner_id, correlation, execution_lease
        raise AssertionError("a reclaimed START must not enter new-turn resume")

    async def inspect_execution_checkpoint(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId,
        checkpoint_cursor: str | None = None,
    ) -> ExecutionCheckpointInspection:
        del thread_id, owner_id
        if checkpoint_cursor is not None and checkpoint_cursor != self._inspection.cursor:
            return ExecutionCheckpointInspection(state=ExecutionCheckpointState.MISSING)
        return self._inspection

    async def recover_execution_checkpoint(
        self,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
        checkpoint_cursor: str,
    ) -> GraphTurn:
        del thread_id, owner_id, correlation, execution_lease, checkpoint_cursor
        raise AssertionError("input replay must be used when no checkpoint exists")


def run_worker_until_after_claim(
    database_url: str,
    schema: str,
    *,
    worker_id: str,
    expected_question: str,
    expected_thread_id: str,
    expected_owner_id: str,
    reached: ProcessSignal,
) -> None:
    """Subprocess entry point intentionally killed while its first lease is held."""

    asyncio.run(
        _run_worker_until_after_claim(
            database_url,
            schema,
            worker_id=worker_id,
            expected_question=expected_question,
            expected_thread_id=expected_thread_id,
            expected_owner_id=expected_owner_id,
            reached=reached,
        ),
        loop_factory=asyncio.SelectorEventLoop,
    )


def run_replacement_worker(
    database_url: str,
    schema: str,
    *,
    worker_id: str,
    job_id: str,
    expected_question: str,
    expected_thread_id: str,
    expected_owner_id: str,
    replayed: ProcessSignal,
) -> None:
    """Subprocess entry point that reclaims and completes the crashed START job."""

    asyncio.run(
        _run_replacement_worker(
            database_url,
            schema,
            worker_id=worker_id,
            job_id=job_id,
            expected_question=expected_question,
            expected_thread_id=expected_thread_id,
            expected_owner_id=expected_owner_id,
            replayed=replayed,
        ),
        loop_factory=asyncio.SelectorEventLoop,
    )


async def _run_worker_until_after_claim(
    database_url: str,
    schema: str,
    *,
    worker_id: str,
    expected_question: str,
    expected_thread_id: str,
    expected_owner_id: str,
    reached: ProcessSignal,
) -> None:
    async with _open_repository(database_url, schema) as repository:
        graph = _ReplayStartGraph(
            expected_question=expected_question,
            expected_thread_id=expected_thread_id,
            expected_owner_id=expected_owner_id,
            replayed=reached,
        )
        worker = DurableExecutionWorker(
            graph,
            repository,
            worker_id=worker_id,
            options=_worker_options(),
            fault_injector=_BlockAfterClaim(reached),
        )
        await worker.start()
        try:
            await worker.wait_for_internal_exit()
        finally:
            await worker.stop()


async def _run_replacement_worker(
    database_url: str,
    schema: str,
    *,
    worker_id: str,
    job_id: str,
    expected_question: str,
    expected_thread_id: str,
    expected_owner_id: str,
    replayed: ProcessSignal,
) -> None:
    async with _open_repository(database_url, schema) as repository:
        worker = DurableExecutionWorker(
            _ReplayStartGraph(
                expected_question=expected_question,
                expected_thread_id=expected_thread_id,
                expected_owner_id=expected_owner_id,
                replayed=replayed,
            ),
            repository,
            worker_id=worker_id,
            options=_worker_options(),
        )
        await worker.start()
        try:
            await _wait_for_completed_job(
                repository,
                job_id=job_id,
                owner_id=expected_owner_id,
            )
        finally:
            await worker.stop()


async def _wait_for_completed_job(
    repository: PostgresConversationRepository,
    *,
    job_id: str,
    owner_id: str,
) -> None:
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        job = await repository.get_execution_job(job_id, owner_id=owner_id)
        if job.state == ExecutionJobState.COMPLETED:
            return
        if job.state == ExecutionJobState.FAILED:
            raise RuntimeError(f"replacement worker failed the reclaimed job: {job.failure_code}")
        await asyncio.sleep(0.02)
    raise TimeoutError("replacement worker did not complete the reclaimed job")


@asynccontextmanager
async def _open_repository(
    database_url: str,
    schema: str,
) -> AsyncIterator[PostgresConversationRepository]:
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
        max_size=2,
        configure=configure,
        open=False,
        name=f"process-chaos-{worker_id_fragment(schema)}",
    )
    try:
        await pool.open(wait=True, timeout=10)
        yield PostgresConversationRepository(
            pool,
            execution_payload_cipher=ExecutionPayloadCipher(PROCESS_EXECUTION_PAYLOAD_KEY),
        )
    finally:
        await pool.close()


def _worker_options() -> ExecutionWorkerOptions:
    return ExecutionWorkerOptions(
        lease_duration=timedelta(seconds=5),
        heartbeat_interval=timedelta(milliseconds=500),
        readiness_timeout=timedelta(seconds=10),
        poll_interval=timedelta(milliseconds=50),
        max_error_backoff=timedelta(milliseconds=500),
        shutdown_grace=timedelta(seconds=5),
    )


def worker_id_fragment(schema: str) -> str:
    return schema[-24:]
