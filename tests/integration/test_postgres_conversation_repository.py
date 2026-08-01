from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationThread,
)
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.domain.errors import (
    AgentPolicyViolationError,
    RunNotFoundError,
    ThreadNotFoundError,
)
from barrier_free_mobility_agent.domain.models import AnswerOutcome
from barrier_free_mobility_agent.domain.usage import (
    ContextReductionReason,
    RunUsage,
    UsageSource,
)
from barrier_free_mobility_agent.persistence.postgres import (
    PostgresConversationRepository,
)
from barrier_free_mobility_agent.persistence.runtime import (
    _configure_postgres_connection,
)

_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests.",
)


@pytest.mark.asyncio
async def test_postgres_repository_survives_restart_isolates_owner_and_deletes() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    other_owner_id = f"other-{suffix}"
    thread = ConversationThread(
        thread_id=f"thread-{suffix}",
        owner_id=owner_id,
        flow_version="mobility-v1",
        state_schema_version="3",
    )
    run = ConversationRun(
        run_id=f"run-{suffix}",
        thread_id=thread.thread_id,
        status=GraphTurnStatus.INTERRUPTED,
        resume_mode=ResumeMode.NEW_TURN,
        outcome=AnswerOutcome.question_too_complex(),
        usage=RunUsage(
            revision=1,
            model_calls=1,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            source=UsageSource.REPORTED,
        ),
    )

    async with _repository(_DATABASE_URL) as first:
        assert first.persistence == "durable"
        assert await first.is_ready() is True
        await first.create_thread(thread, capacity=10)
        stored_thread = await first.add_run(run, owner_id=owner_id, capacity=10)
        assert stored_thread.active_run_id == run.run_id

    async with _repository(_DATABASE_URL) as restarted:
        assert await restarted.get_thread(thread.thread_id, owner_id=owner_id) == stored_thread
        assert await restarted.get_run(run.run_id, owner_id=owner_id) == run
        with pytest.raises(ThreadNotFoundError):
            await restarted.get_thread(thread.thread_id, owner_id=other_owner_id)
        with pytest.raises(RunNotFoundError):
            await restarted.get_run(run.run_id, owner_id=other_owner_id)

        async with (
            await AsyncConnection.connect(_DATABASE_URL) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'agent_runs'
                  AND data_type = 'jsonb'
                ORDER BY column_name
                """
            )
            json_columns = [row["column_name"] for row in await cursor.fetchall()]
            await cursor.execute(
                """
                SELECT is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'agent_runs'
                  AND column_name = 'usage'
                """
            )
            usage_column = await cursor.fetchone()
        assert json_columns == ["context", "outcome", "usage"]
        # Running reservations have no public snapshot yet; terminal rows still validate usage.
        assert usage_column == {"is_nullable": "YES", "column_default": None}

        newer_usage = run.usage.model_copy(
            update={
                "revision": 2,
                "context_reduction_count": 1,
                "reduction_reasons": (ContextReductionReason.OPTIONAL_CONTEXT_REMOVED,),
            }
        )
        updated = await restarted.update_run(
            run.model_copy(update={"usage": newer_usage}),
            owner_id=owner_id,
        )
        assert updated.usage == newer_usage
        stale = await restarted.update_run(run, owner_id=owner_id)
        assert stale.usage == newer_usage
        with pytest.raises(AgentPolicyViolationError) as regression_info:
            await restarted.update_run(
                run.model_copy(update={"usage": RunUsage(revision=3)}),
                owner_id=owner_id,
            )
        assert regression_info.value.context == {"reason": "run_usage_revision_conflict"}

        await restarted.delete_thread(thread.thread_id, owner_id=owner_id)

    async with _repository(_DATABASE_URL) as after_delete:
        with pytest.raises(ThreadNotFoundError):
            await after_delete.get_thread(thread.thread_id, owner_id=owner_id)
        with pytest.raises(RunNotFoundError):
            await after_delete.get_run(run.run_id, owner_id=owner_id)


@asynccontextmanager
async def _repository(
    database_url: str,
) -> AsyncIterator[PostgresConversationRepository]:
    pool = AsyncConnectionPool(
        conninfo=database_url,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        min_size=1,
        max_size=2,
        configure=_configure_postgres_connection,
        open=False,
        name="mobility-agent-integration-test",
    )
    await pool.open(wait=True, timeout=10)
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        repository = PostgresConversationRepository(pool)
        await repository.setup()
        yield repository
    finally:
        await pool.close()
