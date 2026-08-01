from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

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
    RunExecutionPhase,
    ToolFailureCode,
    ToolTransition,
)
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("thread_id", "사용자 질문"),
        ("run_id", "강남역"),
        ("request_id", "raw payload"),
    ],
)
def test_correlation_rejects_free_text_identifiers(field: str, value: str) -> None:
    payload: dict[str, object] = {
        "thread_id": "thread-1",
        "run_id": "run-1",
        "request_id": "request-1",
        "attempt": 1,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        RunExecutionCorrelation.model_validate(payload)


def test_tool_summary_is_frozen_and_rejects_raw_payload_fields() -> None:
    with pytest.raises(ValidationError):
        ResolveStationToolSummary.model_validate(
            {
                "matched": True,
                "candidate_count": 1,
                "clarification_required": False,
                "raw_payload": {"station": "강남역"},
            }
        )

    summary = ResolveStationToolSummary(
        matched=True,
        candidate_count=1,
        clarification_required=False,
    )
    assert summary.model_config["frozen"] is True


@pytest.mark.asyncio
async def test_memory_ledger_records_contiguous_tool_and_terminal_events() -> None:
    repository, correlation = await _reserved_repository()

    handle = await repository.append_tool_started(
        correlation,
        owner_id="owner-a",
        execution_id="execution-1",
        logical_call_id="logical-1",
        attempt=1,
        tool=ToolCapability.RESOLVE_STATION,
    )
    record = await repository.append_tool_terminal(
        handle,
        owner_id="owner-a",
        transition=ToolTransition.COMPLETED,
        duration_ms=15,
        summary=ResolveStationToolSummary(
            matched=True,
            candidate_count=1,
            clarification_required=False,
        ),
    )
    completed = _run(
        correlation.run_id,
        correlation.thread_id,
        status=GraphTurnStatus.COMPLETED,
    )
    assert (
        await repository.finalize_run(
            completed,
            correlation=correlation,
            owner_id="owner-a",
        )
        == completed
    )

    events = await repository.get_run_events(correlation.run_id, owner_id="owner-a")
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.TOOL_STARTED,
        RunEventType.TOOL_COMPLETED,
        RunEventType.RUN_COMPLETED,
    ]
    assert events[2].tool_summary == record.summary
    assert await repository.get_tool_executions(correlation.run_id, owner_id="owner-a") == (record,)

    with pytest.raises(AgentPolicyViolationError):
        await repository.append_tool_started(
            correlation,
            owner_id="owner-a",
            execution_id="execution-2",
            logical_call_id="logical-2",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
        )


@pytest.mark.asyncio
async def test_memory_ledger_concurrently_allocates_gapless_sequences() -> None:
    repository, correlation = await _reserved_repository()

    handles = await asyncio.gather(
        *(
            repository.append_tool_started(
                correlation,
                owner_id="owner-a",
                execution_id=f"execution-{index}",
                logical_call_id=f"logical-{index}",
                attempt=1,
                tool=ToolCapability.RESOLVE_STATION,
            )
            for index in range(1, 5)
        )
    )
    summary = ResolveStationToolSummary(
        matched=False,
        candidate_count=0,
        clarification_required=True,
    )
    await asyncio.gather(
        *(
            repository.append_tool_terminal(
                handle,
                owner_id="owner-a",
                transition=ToolTransition.COMPLETED,
                duration_ms=10,
                summary=summary,
            )
            for handle in handles
        )
    )
    await repository.finalize_run(
        _run(
            correlation.run_id,
            correlation.thread_id,
            status=GraphTurnStatus.COMPLETED,
        ),
        correlation=correlation,
        owner_id="owner-a",
    )

    events = await repository.get_run_events(correlation.run_id, owner_id="owner-a")
    assert [event.sequence for event in events] == list(range(1, 11))
    assert len({event.event_key for event in events}) == len(events)


@pytest.mark.asyncio
async def test_memory_ledger_resume_allocates_attempt_under_lock() -> None:
    repository, initial = await _reserved_repository()
    interrupted = _run(
        initial.run_id,
        initial.thread_id,
        status=GraphTurnStatus.INTERRUPTED,
    )
    await repository.finalize_run(
        interrupted,
        correlation=initial,
        owner_id="owner-a",
    )

    resumed_event = await repository.reserve_resume_attempt(
        initial.run_id,
        request_id="request-2",
        owner_id="owner-a",
    )
    resumed = resumed_event.correlation
    assert resumed.attempt == 2
    assert resumed.request_id == "request-2"
    assert resumed_event.sequence == 3
    assert resumed_event.event_key == "run:2:run.resume_started"

    with pytest.raises(AgentPolicyViolationError):
        await repository.reserve_resume_attempt(
            initial.run_id,
            request_id="request-2",
            owner_id="owner-a",
        )
    with pytest.raises(AgentPolicyViolationError):
        await repository.append_tool_started(
            initial,
            owner_id="owner-a",
            execution_id="stale-execution",
            logical_call_id="logical-stale",
            attempt=1,
            tool=ToolCapability.RESOLVE_STATION,
        )

    completed = _run(
        resumed.run_id,
        resumed.thread_id,
        status=GraphTurnStatus.COMPLETED,
    )
    await repository.finalize_run(
        completed,
        correlation=resumed,
        owner_id="owner-a",
    )
    events = await repository.get_run_events(initial.run_id, owner_id="owner-a")
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert [event.correlation.attempt for event in events] == [1, 1, 2, 2]


@pytest.mark.asyncio
async def test_memory_ledger_requires_started_tool_and_terminal_shape() -> None:
    repository, correlation = await _reserved_repository()
    handle = await repository.append_tool_started(
        correlation,
        owner_id="owner-a",
        execution_id="execution-timeout",
        logical_call_id="logical-timeout",
        attempt=1,
        tool=ToolCapability.RESOLVE_STATION,
    )

    with pytest.raises(AgentPolicyViolationError):
        await repository.finalize_run(
            _run(
                correlation.run_id,
                correlation.thread_id,
                status=GraphTurnStatus.FAILED,
            ),
            correlation=correlation,
            owner_id="owner-a",
        )

    timeout = await repository.append_tool_terminal(
        handle,
        owner_id="owner-a",
        transition=ToolTransition.TIMEOUT,
        duration_ms=45_000,
        error_code=ToolFailureCode.MCP_TIMEOUT,
    )
    assert timeout.transition == ToolTransition.TIMEOUT

    with pytest.raises(AgentPolicyViolationError):
        await repository.append_tool_terminal(
            handle.model_copy(update={"execution_id": "never-started"}),
            owner_id="owner-a",
            transition=ToolTransition.FAILED,
            duration_ms=1,
            error_code=ToolFailureCode.UNEXPECTED_ERROR,
        )


@pytest.mark.asyncio
async def test_memory_ledger_is_owner_scoped_detects_orphans_and_cascades_delete() -> None:
    repository, correlation = await _reserved_repository()
    await repository.append_tool_started(
        correlation,
        owner_id="owner-a",
        execution_id="execution-open",
        logical_call_id="logical-open",
        attempt=1,
        tool=ToolCapability.RESOLVE_STATION,
    )

    assert (
        await repository.find_orphaned_runs(
            owner_id="owner-b",
            stale_before=datetime.now(UTC) + timedelta(seconds=1),
        )
        == ()
    )
    orphans = await repository.find_orphaned_runs(
        owner_id="owner-a",
        stale_before=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert len(orphans) == 1
    assert orphans[0].phase == RunExecutionPhase.RUNNING
    assert orphans[0].correlation.attempt == 1
    assert orphans[0].reasons == (
        OrphanReason.RUN_STALE,
        OrphanReason.TOOL_EXECUTION_STALE,
    )
    assert orphans[0].open_tool_execution_ids == ("execution-open",)

    with pytest.raises(RunNotFoundError):
        await repository.get_run_events(correlation.run_id, owner_id="owner-b")

    await repository.delete_thread(correlation.thread_id, owner_id="owner-a")
    with pytest.raises(RunNotFoundError):
        await repository.get_run_events(correlation.run_id, owner_id="owner-a")


@pytest.mark.asyncio
async def test_initial_reservation_checks_owner_before_run_conflict() -> None:
    repository, correlation = await _reserved_repository()

    with pytest.raises(ThreadNotFoundError):
        await repository.reserve_initial_run(
            correlation,
            owner_id="owner-b",
            capacity=3,
        )


async def _reserved_repository() -> tuple[MemoryConversationRepository, RunExecutionCorrelation]:
    repository = MemoryConversationRepository()
    await repository.create_thread(
        ConversationThread(
            thread_id="thread-1",
            owner_id="owner-a",
            flow_version="mobility-v2",
            state_schema_version="3",
        ),
        capacity=3,
    )
    await repository.create_thread(
        ConversationThread(
            thread_id="thread-2",
            owner_id="owner-b",
            flow_version="mobility-v2",
            state_schema_version="3",
        ),
        capacity=3,
    )
    correlation = RunExecutionCorrelation(
        thread_id="thread-1",
        run_id="run-1",
        request_id="request-1",
        attempt=1,
    )
    event = await repository.reserve_initial_run(
        correlation,
        owner_id="owner-a",
        capacity=3,
    )
    assert event.sequence == 1
    assert event.event_type == RunEventType.RUN_STARTED
    assert event.correlation == correlation
    return repository, correlation


def _run(
    run_id: str,
    thread_id: str,
    *,
    status: GraphTurnStatus,
) -> ConversationRun:
    return ConversationRun(
        run_id=run_id,
        thread_id=thread_id,
        status=status,
        resume_mode=(
            ResumeMode.NEW_TURN if status == GraphTurnStatus.INTERRUPTED else ResumeMode.NONE
        ),
        outcome=AnswerOutcome(
            source=AnswerSource.AGENT,
            status=(
                ResponseStatus.NEEDS_CLARIFICATION
                if status == GraphTurnStatus.INTERRUPTED
                else ResponseStatus.FAILED
            ),
            intent=QuestionIntent.UNKNOWN,
            user_message="테스트 projection",
            clarification_needed=status == GraphTurnStatus.INTERRUPTED,
            questions=(
                ["추가 조건을 확인해 주세요."] if status == GraphTurnStatus.INTERRUPTED else []
            ),
            evidence=AnswerEvidence(),
        ),
    )
