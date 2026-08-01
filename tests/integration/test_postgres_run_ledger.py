from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.errors import RaiseException
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
from barrier_free_mobility_agent.domain.routing import ToolCapability
from barrier_free_mobility_agent.domain.run_ledger import (
    OrphanReason,
    ResolveStationToolSummary,
    RunEventType,
    RunExecutionCorrelation,
    ToolTransition,
)
from barrier_free_mobility_agent.persistence.postgres import (
    REPOSITORY_SCHEMA_VERSION,
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
async def test_postgres_initial_reservation_is_atomic_and_owner_scoped() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"
    correlations = tuple(
        RunExecutionCorrelation(
            thread_id=thread_id,
            run_id=f"run-{index}-{suffix}",
            request_id=f"request-{index}-{suffix}",
            attempt=1,
        )
        for index in range(2)
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(
            _thread(thread_id, owner_id),
            capacity=10,
        )
        unauthorized = correlations[0].model_copy(
            update={"run_id": f"unauthorized-{suffix}", "attempt": 2}
        )
        with pytest.raises(ThreadNotFoundError):
            await repository.reserve_initial_run(
                unauthorized,
                owner_id=f"other-{owner_id}",
                capacity=10,
            )
        results = await asyncio.gather(
            *(
                repository.reserve_initial_run(
                    correlation,
                    owner_id=owner_id,
                    capacity=10,
                )
                for correlation in correlations
            ),
            return_exceptions=True,
        )
        events = [result for result in results if not isinstance(result, BaseException)]
        failures = [result for result in results if isinstance(result, BaseException)]
        assert len(events) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], ThreadBusyError)
        reserved = events[0]

        with pytest.raises(RunNotFoundError):
            await repository.get_run_events(
                reserved.correlation.run_id,
                owner_id=f"other-{owner_id}",
            )

        rows = await _fetch_all(
            _DATABASE_URL,
            """
            SELECT
                t.active_run_id,
                t.run_count,
                r.run_id,
                r.execution_state,
                r.execution_attempt,
                r.next_event_sequence,
                e.sequence,
                e.event_type
            FROM agent_threads AS t
            JOIN agent_runs AS r ON r.thread_id = t.thread_id
            JOIN run_events AS e ON e.run_id = r.run_id
            WHERE t.thread_id = %s
            """,
            (thread_id,),
        )
        assert rows == [
            {
                "active_run_id": reserved.correlation.run_id,
                "run_count": 1,
                "run_id": reserved.correlation.run_id,
                "execution_state": "running",
                "execution_attempt": 1,
                "next_event_sequence": 2,
                "sequence": 1,
                "event_type": "run.started",
            }
        ]
        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_legacy_update_cannot_mutate_ledger_managed_run() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"
    run_id = f"run-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=run_id,
        request_id=f"request-{suffix}",
        attempt=1,
    )
    original = _run(
        run_id,
        thread_id,
        status=GraphTurnStatus.INTERRUPTED,
        user_message="original public snapshot",
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        await repository.reserve_initial_run(
            correlation,
            owner_id=owner_id,
            capacity=10,
        )
        await repository.finalize_run(
            original,
            correlation=correlation,
            owner_id=owner_id,
        )

        with pytest.raises(AgentPolicyViolationError) as error_info:
            await repository.update_run(
                original.model_copy(
                    update={
                        "outcome": original.outcome.model_copy(
                            update={"user_message": "mutated without a ledger event"}
                        )
                    }
                ),
                owner_id=owner_id,
            )
        assert error_info.value.context == {"reason": "run_ledger_finalize_required"}
        assert await repository.get_run(run_id, owner_id=owner_id) == original
        assert [
            event.event_type for event in await repository.get_run_events(run_id, owner_id=owner_id)
        ] == [RunEventType.RUN_STARTED, RunEventType.RUN_INTERRUPTED]

        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_ledger_survives_restart_redacts_and_cascades() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    other_owner_id = f"other-{suffix}"
    thread_id = f"thread-{suffix}"
    run_id = f"run-{suffix}"
    sentinel_question = "SENTINEL_RAW_QUESTION_절대저장금지"
    sentinel_station = "SENTINEL_STATION_강남역"
    initial = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=run_id,
        request_id=f"request-1-{suffix}",
        attempt=1,
    )

    async with _repository(_DATABASE_URL) as first:
        await first.create_thread(_thread(thread_id, owner_id), capacity=10)
        started = await first.reserve_initial_run(
            initial,
            owner_id=owner_id,
            capacity=10,
        )
        assert started.sequence == 1
        first_handle = await first.append_tool_started(
            initial,
            owner_id=owner_id,
            execution_id=f"execution-1-{suffix}",
            logical_call_id="trip.resolve-origin",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
        )
        await first.append_tool_terminal(
            first_handle,
            owner_id=owner_id,
            transition=ToolTransition.COMPLETED,
            duration_ms=12,
            summary=_resolution_summary(),
        )
        await first.finalize_run(
            _run(
                run_id,
                thread_id,
                status=GraphTurnStatus.INTERRUPTED,
                user_message=f"{sentinel_question} {sentinel_station}",
            ),
            correlation=initial,
            owner_id=owner_id,
        )
        assert [
            event.sequence for event in await first.get_run_events(run_id, owner_id=owner_id)
        ] == [1, 2, 3, 4]

    async with _repository(_DATABASE_URL) as restarted:
        resume_event = await restarted.reserve_resume_attempt(
            run_id,
            request_id=f"request-2-{suffix}",
            owner_id=owner_id,
        )
        resumed = resume_event.correlation
        assert resumed.attempt == 2
        assert resume_event.sequence == 5
        second_handle = await restarted.append_tool_started(
            resumed,
            owner_id=owner_id,
            execution_id=f"execution-2-{suffix}",
            logical_call_id="trip.resolve-destination",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
        )

        with pytest.raises(RunNotFoundError):
            await restarted.get_run_events(run_id, owner_id=other_owner_id)
        with pytest.raises(RunNotFoundError):
            await restarted.get_tool_executions(run_id, owner_id=other_owner_id)
        assert (
            await restarted.find_orphaned_runs(
                owner_id=other_owner_id,
                stale_before=datetime.now(UTC) + timedelta(seconds=1),
            )
            == ()
        )

        orphans = await restarted.find_orphaned_runs(
            owner_id=owner_id,
            stale_before=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert len(orphans) == 1
        assert orphans[0].correlation == resumed
        assert orphans[0].reasons == (
            OrphanReason.RUN_STALE,
            OrphanReason.TOOL_EXECUTION_STALE,
        )
        assert orphans[0].open_tool_execution_ids == (second_handle.execution_id,)

        await restarted.append_tool_terminal(
            second_handle,
            owner_id=owner_id,
            transition=ToolTransition.COMPLETED,
            duration_ms=18,
            summary=_resolution_summary(),
        )
        await restarted.finalize_run(
            _run(
                run_id,
                thread_id,
                status=GraphTurnStatus.COMPLETED,
                user_message=f"{sentinel_question} {sentinel_station}",
            ),
            correlation=resumed,
            owner_id=owner_id,
        )
        events = await restarted.get_run_events(run_id, owner_id=owner_id)
        executions = await restarted.get_tool_executions(run_id, owner_id=owner_id)
        assert [event.sequence for event in events] == list(range(1, 9))
        assert [event.event_type for event in events] == [
            RunEventType.RUN_STARTED,
            RunEventType.TOOL_STARTED,
            RunEventType.TOOL_COMPLETED,
            RunEventType.RUN_INTERRUPTED,
            RunEventType.RUN_RESUME_STARTED,
            RunEventType.TOOL_STARTED,
            RunEventType.TOOL_COMPLETED,
            RunEventType.RUN_COMPLETED,
        ]
        assert [record.terminal_event_sequence for record in executions] == [3, 7]

        ledger_text = await _ledger_payload_text(_DATABASE_URL, run_id)
        assert sentinel_question not in ledger_text
        assert sentinel_station not in ledger_text

        await restarted.delete_thread(thread_id, owner_id=owner_id)

    counts = await _fetch_one(
        _DATABASE_URL,
        """
        SELECT
            (SELECT count(*) FROM agent_runs WHERE run_id = %s) AS runs,
            (SELECT count(*) FROM run_events WHERE run_id = %s) AS events,
            (SELECT count(*) FROM tool_executions WHERE run_id = %s) AS tools
        """,
        (run_id, run_id, run_id),
    )
    assert counts == {"runs": 0, "events": 0, "tools": 0}


@pytest.mark.asyncio
async def test_postgres_ledger_rows_reject_updates() -> None:
    assert _DATABASE_URL is not None
    suffix = uuid4().hex
    owner_id = f"owner-{suffix}"
    thread_id = f"thread-{suffix}"
    run_id = f"run-{suffix}"
    correlation = RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=run_id,
        request_id=f"request-{suffix}",
        attempt=1,
    )

    async with _repository(_DATABASE_URL) as repository:
        await repository.create_thread(_thread(thread_id, owner_id), capacity=10)
        await repository.reserve_initial_run(
            correlation,
            owner_id=owner_id,
            capacity=10,
        )
        handle = await repository.append_tool_started(
            correlation,
            owner_id=owner_id,
            execution_id=f"execution-{suffix}",
            logical_call_id="trip.resolve-origin",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
        )
        await repository.append_tool_terminal(
            handle,
            owner_id=owner_id,
            transition=ToolTransition.COMPLETED,
            duration_ms=10,
            summary=_resolution_summary(),
        )

        connection = await AsyncConnection.connect(
            _DATABASE_URL,
            autocommit=True,
            row_factory=dict_row,
        )
        try:
            with pytest.raises(RaiseException):
                await connection.execute(
                    "UPDATE run_events SET event_type = event_type WHERE run_id = %s",
                    (run_id,),
                )
            with pytest.raises(RaiseException):
                await connection.execute(
                    "UPDATE tool_executions "
                    "SET terminal_status = terminal_status WHERE run_id = %s",
                    (run_id,),
                )
        finally:
            await connection.close()

        assert [
            event.event_type for event in await repository.get_run_events(run_id, owner_id=owner_id)
        ] == [
            RunEventType.RUN_STARTED,
            RunEventType.TOOL_STARTED,
            RunEventType.TOOL_COMPLETED,
        ]
        await repository.delete_thread(thread_id, owner_id=owner_id)


@pytest.mark.asyncio
async def test_postgres_repository_schema_nine_has_ledger_and_queue_contract() -> None:
    assert _DATABASE_URL is not None
    async with _repository(_DATABASE_URL):
        assert REPOSITORY_SCHEMA_VERSION == 9

    version = await _fetch_one(
        _DATABASE_URL,
        "SELECT max(version) AS version FROM agent_repository_migrations",
    )
    assert version == {"version": 9}

    tables = await _fetch_all(
        _DATABASE_URL,
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name IN (
              'agent_runs',
              'run_events',
              'run_execution_jobs',
              'tool_executions'
          )
        ORDER BY table_name
        """,
    )
    assert [row["table_name"] for row in tables] == [
        "agent_runs",
        "run_events",
        "run_execution_jobs",
        "tool_executions",
    ]

    columns = await _fetch_all(
        _DATABASE_URL,
        """
        SELECT table_name, column_name, is_nullable, data_type
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND (
            (table_name = 'agent_runs' AND column_name IN (
                'execution_state', 'initial_request_id',
                'execution_attempt', 'next_event_sequence'
            ))
            OR (table_name = 'run_events' AND column_name IN (
                'event_id', 'event_key', 'sequence', 'payload'
            ))
            OR (table_name = 'tool_executions' AND column_name IN (
                'execution_id', 'logical_call_id', 'attempt',
                'started_event_sequence', 'terminal_event_sequence', 'payload'
            ))
          )
        ORDER BY table_name, column_name
        """,
    )
    column_keys = {(row["table_name"], row["column_name"]) for row in columns}
    assert column_keys == {
        ("agent_runs", "execution_attempt"),
        ("agent_runs", "execution_state"),
        ("agent_runs", "initial_request_id"),
        ("agent_runs", "next_event_sequence"),
        ("run_events", "event_id"),
        ("run_events", "event_key"),
        ("run_events", "payload"),
        ("run_events", "sequence"),
        ("tool_executions", "attempt"),
        ("tool_executions", "execution_id"),
        ("tool_executions", "logical_call_id"),
        ("tool_executions", "payload"),
        ("tool_executions", "started_event_sequence"),
        ("tool_executions", "terminal_event_sequence"),
    }
    assert all(row["is_nullable"] == "NO" for row in columns)

    constraints = await _fetch_all(
        _DATABASE_URL,
        """
        SELECT conname
        FROM pg_constraint
        WHERE connamespace = current_schema()::regnamespace
          AND conrelid IN (
              'agent_runs'::regclass,
              'run_events'::regclass,
              'tool_executions'::regclass
          )
        """,
    )
    constraint_names = {row["conname"] for row in constraints}
    assert {
        "agent_runs_execution_state",
        "agent_runs_execution_projection_consistency",
        "run_events_run_fk",
        "run_events_run_sequence_unique",
        "run_events_run_event_key_unique",
        "tool_executions_run_fk",
        "tool_executions_started_event_fk",
        "tool_executions_terminal_event_fk",
        "tool_executions_logical_attempt_unique",
    } <= constraint_names

    indexes = await _fetch_all(
        _DATABASE_URL,
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename IN ('run_events', 'tool_executions')
        """,
    )
    assert {
        "run_events_run_sequence_idx",
        "run_events_tool_attempt_started_unique",
        "tool_executions_run_completed_idx",
    } <= {row["indexname"] for row in indexes}

    triggers = await _fetch_all(
        _DATABASE_URL,
        """
        SELECT tgname
        FROM pg_trigger
        WHERE NOT tgisinternal
          AND tgrelid IN ('run_events'::regclass, 'tool_executions'::regclass)
        """,
    )
    assert {row["tgname"] for row in triggers} == {
        "run_events_reject_update",
        "tool_executions_reject_update",
    }


def _thread(thread_id: str, owner_id: str) -> ConversationThread:
    return ConversationThread(
        thread_id=thread_id,
        owner_id=owner_id,
        flow_version="mobility-v2",
        state_schema_version="3",
    )


def _run(
    run_id: str,
    thread_id: str,
    *,
    status: GraphTurnStatus,
    user_message: str,
) -> ConversationRun:
    interrupted = status == GraphTurnStatus.INTERRUPTED
    return ConversationRun(
        run_id=run_id,
        thread_id=thread_id,
        status=status,
        resume_mode=ResumeMode.NEW_TURN if interrupted else ResumeMode.NONE,
        outcome=AnswerOutcome(
            source=AnswerSource.AGENT,
            status=(ResponseStatus.NEEDS_CLARIFICATION if interrupted else ResponseStatus.FAILED),
            intent=QuestionIntent.UNKNOWN,
            user_message=user_message,
            clarification_needed=interrupted,
            questions=["추가 확인"] if interrupted else [],
            evidence=AnswerEvidence(),
        ),
    )


def _resolution_summary() -> ResolveStationToolSummary:
    return ResolveStationToolSummary(
        matched=True,
        candidate_count=1,
        clarification_required=False,
    )


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
        max_size=4,
        configure=_configure_postgres_connection,
        open=False,
        name="mobility-agent-run-ledger-test",
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


async def _fetch_one(
    database_url: str,
    query: str,
    params: tuple[object, ...] = (),
) -> dict[str, object]:
    rows = await _fetch_all(database_url, query, params)
    assert len(rows) == 1
    return rows[0]


async def _fetch_all(
    database_url: str,
    query: str,
    params: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    connection = await AsyncConnection.connect(database_url, row_factory=dict_row)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]
    finally:
        await connection.close()


async def _ledger_payload_text(database_url: str, run_id: str) -> str:
    row = await _fetch_one(
        database_url,
        """
        SELECT
            coalesce((
                SELECT string_agg(payload::text, '')
                FROM run_events
                WHERE run_id = %s
            ), '') || coalesce((
                SELECT string_agg(payload::text, '')
                FROM tool_executions
                WHERE run_id = %s
            ), '') AS payload_text
        """,
        (run_id, run_id),
    )
    return str(row["payload_text"])
