from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from psycopg import AsyncCursor
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationThread,
)
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.domain.errors import (
    AgentCapacityExceededError,
    AgentPolicyViolationError,
    AgentStateIncompatibleError,
    RunNotFoundError,
    ThreadBusyError,
    ThreadNotFoundError,
)
from barrier_free_mobility_agent.domain.models import (
    AnswerEvidence,
    AnswerOutcome,
    AnswerSource,
    QuestionIntent,
    ResponseStatus,
)
from barrier_free_mobility_agent.domain.usage import (
    ContextReductionReason,
    RunUsage,
    UsageSource,
)
from barrier_free_mobility_agent.persistence.memory import (
    MemoryConversationRepository,
)
from barrier_free_mobility_agent.persistence.postgres import (
    _MIGRATIONS,
    REPOSITORY_SCHEMA_VERSION,
    PostgresConversationRepository,
    _quarantine_incompatible_execution_job,
    _run_from_row,
    _usage_from_payload,
)


@pytest.mark.asyncio
async def test_incompatible_execution_quarantine_uses_stable_safe_terminal_code() -> None:
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(return_value={"job_id": "job-1"})
    sentinel = "PRIVATE_PAYLOAD_MUST_NOT_APPEAR"

    await _quarantine_incompatible_execution_job(
        cast(AsyncCursor[DictRow], cursor),
        job_id="job-1",
        token="lease-token",
        worker_id="worker-1",
    )

    query, parameters = cursor.execute.await_args.args
    assert "EXECUTION_PAYLOAD_INCOMPATIBLE" not in query
    assert parameters[0] == "EXECUTION_PAYLOAD_INCOMPATIBLE"
    assert "payload_ciphertext = NULL" in query
    assert sentinel not in repr(cursor.execute.await_args)


@pytest.mark.asyncio
async def test_memory_repository_enforces_owner_scoped_thread_capacity() -> None:
    repository = MemoryConversationRepository()
    first = _thread("thread-1", "owner-a")
    other_owner = _thread("thread-2", "owner-b")

    assert await repository.create_thread(first, capacity=1) == first
    assert await repository.create_thread(other_owner, capacity=1) == other_owner
    with pytest.raises(AgentCapacityExceededError):
        await repository.create_thread(_thread("thread-3", "owner-a"), capacity=1)

    with pytest.raises(ThreadNotFoundError):
        await repository.get_thread(first.thread_id, owner_id="owner-b")
    assert repository.persistence == "ephemeral"
    assert await repository.is_ready() is True


@pytest.mark.asyncio
async def test_memory_repository_capacity_check_is_atomic() -> None:
    repository = MemoryConversationRepository()

    results = await asyncio.gather(
        repository.create_thread(_thread("thread-a", "owner"), capacity=1),
        repository.create_thread(_thread("thread-b", "owner"), capacity=1),
        return_exceptions=True,
    )

    assert sum(isinstance(item, ConversationThread) for item in results) == 1
    assert sum(isinstance(item, AgentCapacityExceededError) for item in results) == 1


@pytest.mark.asyncio
async def test_memory_repository_enforces_global_thread_capacity() -> None:
    repository = MemoryConversationRepository()
    await repository.create_thread(
        _thread("thread-a", "owner-a"),
        capacity=10,
        total_capacity=1,
    )

    with pytest.raises(AgentCapacityExceededError) as exc_info:
        await repository.create_thread(
            _thread("thread-b", "owner-b"),
            capacity=10,
            total_capacity=1,
        )

    assert exc_info.value.context == {"resource": "threads_total", "limit": 1}


@pytest.mark.asyncio
async def test_memory_repository_atomically_tracks_active_run_and_run_capacity() -> None:
    repository = MemoryConversationRepository()
    thread = await repository.create_thread(
        _thread("thread-1", "owner"),
        capacity=2,
    )
    active = _run("run-1", thread.thread_id, resumable=True)

    after_active = await repository.add_run(
        active,
        owner_id="owner",
        capacity=2,
    )
    assert after_active.active_run_id == active.run_id
    assert after_active.run_count == 1

    with pytest.raises(ThreadBusyError):
        await repository.add_run(
            _run("run-2", thread.thread_id),
            owner_id="owner",
            capacity=2,
        )

    terminal = _run("run-1", thread.thread_id)
    await repository.update_run(terminal, owner_id="owner")
    after_terminal = await repository.get_thread(thread.thread_id, owner_id="owner")
    assert after_terminal.active_run_id is None

    await repository.add_run(
        _run("run-2", thread.thread_id),
        owner_id="owner",
        capacity=2,
    )
    with pytest.raises(AgentCapacityExceededError):
        await repository.add_run(
            _run("run-3", thread.thread_id),
            owner_id="owner",
            capacity=2,
        )


@pytest.mark.asyncio
async def test_memory_repository_hides_runs_from_other_owners_and_returns_copies() -> None:
    repository = MemoryConversationRepository()
    thread = await repository.create_thread(
        _thread("thread-1", "owner-a"),
        capacity=2,
    )
    run = _run("run-1", thread.thread_id)
    await repository.add_run(run, owner_id="owner-a", capacity=2)

    with pytest.raises(RunNotFoundError):
        await repository.get_run(run.run_id, owner_id="owner-b")
    with pytest.raises(RunNotFoundError):
        await repository.update_run(run, owner_id="owner-b")

    first = await repository.get_run(run.run_id, owner_id="owner-a")
    first.outcome.user_message = "caller mutation"
    second = await repository.get_run(run.run_id, owner_id="owner-a")
    assert second.outcome.user_message == "처리하지 못했습니다."


@pytest.mark.asyncio
async def test_memory_repository_delete_is_owner_checked_and_cascades_runs() -> None:
    repository = MemoryConversationRepository()
    thread = await repository.create_thread(
        _thread("thread-1", "owner-a"),
        capacity=2,
    )
    run = _run("run-1", thread.thread_id)
    await repository.add_run(run, owner_id="owner-a", capacity=2)

    with pytest.raises(ThreadNotFoundError):
        await repository.delete_thread(thread.thread_id, owner_id="owner-b")

    await repository.delete_thread(thread.thread_id, owner_id="owner-a")
    with pytest.raises(ThreadNotFoundError):
        await repository.get_thread(thread.thread_id, owner_id="owner-a")
    with pytest.raises(RunNotFoundError):
        await repository.get_run(run.run_id, owner_id="owner-a")


@pytest.mark.asyncio
async def test_memory_repository_prevents_usage_revision_regression_and_conflict() -> None:
    repository = MemoryConversationRepository()
    thread = await repository.create_thread(_thread("thread-1", "owner"), capacity=2)
    current_usage = _usage(revision=2, input_tokens=100)
    run = _run("run-1", thread.thread_id, usage=current_usage)
    await repository.add_run(run, owner_id="owner", capacity=2)

    stale = _run("run-1", thread.thread_id, usage=RunUsage(revision=1))
    preserved = await repository.update_run(stale, owner_id="owner")
    assert preserved.usage == current_usage
    assert (await repository.get_run(run.run_id, owner_id="owner")).usage == current_usage

    conflicting = _run(
        "run-1",
        thread.thread_id,
        usage=current_usage.model_copy(
            update={
                "context_reduction_count": 1,
                "reduction_reasons": (ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,),
            }
        ),
    )
    with pytest.raises(AgentPolicyViolationError) as exc_info:
        await repository.update_run(conflicting, owner_id="owner")
    assert exc_info.value.context == {"reason": "run_usage_revision_conflict"}

    regressed = _run(
        "run-1",
        thread.thread_id,
        usage=RunUsage(revision=3),
    )
    with pytest.raises(AgentPolicyViolationError) as regression_info:
        await repository.update_run(regressed, owner_id="owner")
    assert regression_info.value.context == {"reason": "run_usage_revision_conflict"}


def test_postgres_run_payload_version_fails_before_model_decoding() -> None:
    row = cast(DictRow, {"payload_schema_version": 999})

    with pytest.raises(AgentStateIncompatibleError):
        _run_from_row(row)


def test_postgres_usage_version_fails_before_run_model_decoding() -> None:
    with pytest.raises(AgentStateIncompatibleError):
        _usage_from_payload({"schema_version": "999", "raw_prompt": "must-not-leak"})

    with pytest.raises(AgentStateIncompatibleError):
        _usage_from_payload({"schema_version": "1", "input_tokens": -1})


def test_repository_schema_retains_schema_four_usage_migration() -> None:
    assert REPOSITORY_SCHEMA_VERSION == 9
    migration = dict(_MIGRATIONS)[4]
    normalized = [" ".join(statement.split()) for statement in migration]

    assert "ADD COLUMN usage JSONB NULL" in normalized[0]
    assert "UPDATE agent_runs SET usage" in normalized[1]
    assert '"source": "none"' in normalized[1]
    assert '"reduction_reasons": []' in normalized[1]
    assert "CHECK (jsonb_typeof(usage) = 'object')" in normalized[2]
    assert "ALTER COLUMN usage SET NOT NULL" in normalized[3]
    assert "ALTER COLUMN usage DROP DEFAULT" in normalized[4]


def test_repository_schema_five_adds_append_only_run_and_tool_ledger() -> None:
    migration = dict(_MIGRATIONS)[5]
    normalized = [" ".join(statement.split()) for statement in migration]

    assert "ADD COLUMN execution_state TEXT NULL" in normalized[0]
    assert "ALTER COLUMN status DROP NOT NULL" in normalized[2]
    assert "CREATE TABLE run_events" in normalized[3]
    assert "UNIQUE (run_id, sequence)" in normalized[3]
    assert "CREATE UNIQUE INDEX run_events_tool_attempt_started_unique" in normalized[5]
    assert "CREATE TABLE tool_executions" in normalized[6]
    assert "terminal_event_sequence > started_event_sequence" in normalized[6]
    assert "CREATE TRIGGER run_events_reject_update" in normalized[9]
    assert "CREATE TRIGGER tool_executions_reject_update" in normalized[10]


def test_repository_schema_six_adds_fenced_execution_jobs() -> None:
    migration = dict(_MIGRATIONS)[6]
    normalized = [" ".join(statement.split()) for statement in migration]
    table = next(
        statement for statement in normalized if "CREATE TABLE run_execution_jobs" in statement
    )

    assert "agent_threads_thread_owner_unique" in normalized[0]
    assert "CHECK (payload_schema_version = 1)" in table
    assert "UNIQUE (owner_id, idempotency_digest)" in table
    assert "UNIQUE (run_id, execution_attempt)" in table
    assert "lease_expires_at > heartbeat_at" in table
    assert "run_execution_jobs_thread_owner_fk" in table
    assert any("CREATE INDEX run_execution_jobs_queued_claim_idx" in sql for sql in normalized)
    assert any("CREATE INDEX run_execution_jobs_expired_lease_idx" in sql for sql in normalized)


def test_repository_schema_seven_encrypts_private_execution_payloads() -> None:
    migration = dict(_MIGRATIONS)[7]
    normalized = [" ".join(statement.split()) for statement in migration]

    assert "ADD COLUMN payload_ciphertext BYTEA NULL" in normalized[0]
    assert "PAYLOAD_ENCRYPTION_MIGRATION_REQUIRED" in normalized[1]
    assert "DROP CONSTRAINT run_execution_jobs_state_shape" in normalized[2]
    assert "run_execution_jobs_plaintext_payload_forbidden" in normalized[3]
    assert "run_execution_jobs_encrypted_payload_shape" in normalized[3]


def test_repository_schema_eight_adds_thread_deletion_fence() -> None:
    migration = dict(_MIGRATIONS)[8]
    normalized = [" ".join(statement.split()) for statement in migration]

    assert "ADD COLUMN deletion_token TEXT NULL" in normalized[0]
    assert "agent_threads_deletion_shape" in normalized[0]
    assert "agent_threads_deletion_pending_idx" in normalized[1]


def test_repository_schema_nine_adds_fenced_worker_registry() -> None:
    migration = dict(_MIGRATIONS)[9]
    normalized = [" ".join(statement.split()) for statement in migration]

    assert "CREATE TABLE execution_worker_registry" in normalized[0]
    assert "token TEXT NOT NULL UNIQUE" in normalized[0]
    assert "CHECK (generation >= 1)" in normalized[0]
    assert "state IN ('ready', 'draining')" in normalized[0]
    assert "expires_at > heartbeat_at" in normalized[0]
    assert "execution_worker_registry_ready_idx" in normalized[1]
    assert "WHERE state = 'ready'" in normalized[1]


@pytest.mark.asyncio
async def test_postgres_readiness_requires_every_dml_privilege() -> None:
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(
        return_value=[{"name": "agent_threads", "relation": "agent_threads"}]
    )
    cursor.fetchone = AsyncMock(return_value={"ready": True})
    pool = _postgres_readiness_pool(cursor)
    repository = PostgresConversationRepository(
        cast(AsyncConnectionPool[Any], pool),
        ready_timeout_seconds=1,
    )

    assert await repository.is_ready() is True

    privilege_query = cursor.execute.await_args_list[1].args[0]
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert f"has_table_privilege(current_user, name, '{privilege}')" in privilege_query


@pytest.mark.asyncio
async def test_postgres_readiness_timeout_covers_the_whole_probe() -> None:
    async def slow_query(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(0.1)

    cursor = MagicMock()
    cursor.execute = AsyncMock(side_effect=slow_query)
    pool = _postgres_readiness_pool(cursor)
    repository = PostgresConversationRepository(
        cast(AsyncConnectionPool[Any], pool),
        ready_timeout_seconds=0.01,
    )

    assert await repository.is_ready() is False


def _thread(thread_id: str, owner_id: str) -> ConversationThread:
    return ConversationThread(
        thread_id=thread_id,
        owner_id=owner_id,
        flow_version="mobility-v1",
        state_schema_version="3",
    )


def _run(
    run_id: str,
    thread_id: str,
    *,
    resumable: bool = False,
    usage: RunUsage | None = None,
) -> ConversationRun:
    if resumable:
        return ConversationRun(
            run_id=run_id,
            thread_id=thread_id,
            status=GraphTurnStatus.INTERRUPTED,
            resume_mode=ResumeMode.NEW_TURN,
            outcome=AnswerOutcome.question_too_complex(),
            usage=usage or RunUsage(),
        )
    return ConversationRun(
        run_id=run_id,
        thread_id=thread_id,
        status=GraphTurnStatus.COMPLETED,
        resume_mode=ResumeMode.NONE,
        outcome=AnswerOutcome(
            source=AnswerSource.AGENT,
            status=ResponseStatus.FAILED,
            intent=QuestionIntent.UNKNOWN,
            user_message="처리하지 못했습니다.",
            evidence=AnswerEvidence(),
        ),
        usage=usage or RunUsage(),
    )


def _usage(*, revision: int, input_tokens: int) -> RunUsage:
    return RunUsage(
        revision=revision,
        model_calls=1,
        input_tokens=input_tokens,
        output_tokens=0,
        total_tokens=input_tokens,
        source=UsageSource.REPORTED,
    )


def _postgres_readiness_pool(cursor: MagicMock) -> MagicMock:
    cursor_context = MagicMock()
    cursor_context.__aenter__ = AsyncMock(return_value=cursor)
    cursor_context.__aexit__ = AsyncMock(return_value=False)
    connection = MagicMock()
    connection.cursor.return_value = cursor_context
    connection_context = MagicMock()
    connection_context.__aenter__ = AsyncMock(return_value=connection)
    connection_context.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = connection_context
    return pool
