from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.errors import NodeCancelledError
from tests.factories import QUESTION, facility_question_result
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.errors import McpTimeoutError
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    ExecutionLease,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    AccessibilityResult,
    MobilityProfile,
    StationResolutionResult,
)
from barrier_free_mobility_agent.domain.routing import ToolCapability
from barrier_free_mobility_agent.domain.run_ledger import (
    AnswerFacilityQuestionToolSummary,
    GenerateAccessibilityBriefToolSummary,
    ResolveStationToolSummary,
    RunEventRecord,
    RunExecutionCorrelation,
    ToolExecutionHandle,
    ToolExecutionRecord,
    ToolExecutionSummary,
    ToolFailureCode,
    ToolTransition,
)


class RecordingRunLedger:
    def __init__(
        self,
        order: list[str],
        *,
        block_started: bool = False,
        terminal_error: Exception | None = None,
    ) -> None:
        self.order = order
        self.terminal_error = terminal_error
        self.started_entered = asyncio.Event()
        self.release_started = asyncio.Event()
        if not block_started:
            self.release_started.set()
        self.handles: list[ToolExecutionHandle] = []
        self.terminals: list[ToolExecutionRecord] = []
        self._sequence = 0

    async def reserve_initial_run(
        self,
        correlation: RunExecutionCorrelation,
        *,
        owner_id: str,
        capacity: int,
    ) -> RunEventRecord:
        raise AssertionError("The graph must not reserve conversation runs.")

    async def reserve_resume_attempt(
        self,
        run_id: str,
        *,
        request_id: str,
        owner_id: str,
    ) -> RunEventRecord:
        raise AssertionError("The graph must not reserve conversation runs.")

    async def finalize_run(
        self,
        run: ConversationRun,
        *,
        correlation: RunExecutionCorrelation,
        owner_id: str,
    ) -> ConversationRun:
        raise AssertionError("The graph must not finalize conversation runs.")

    async def finalize_execution(
        self,
        run: ConversationRun,
        *,
        correlation: RunExecutionCorrelation,
        owner_id: str,
        execution_lease: ExecutionLease,
        job_state: ExecutionJobState,
        checkpoint_cursor: str | None = None,
        failure_code: str | None = None,
    ) -> ConversationRun:
        raise AssertionError("The graph must not finalize conversation runs.")

    async def append_tool_started(
        self,
        correlation: RunExecutionCorrelation,
        *,
        owner_id: str,
        execution_id: str,
        logical_call_id: str,
        attempt: int,
        tool: ToolCapability,
        execution_lease: ExecutionLease | None = None,
    ) -> ToolExecutionHandle:
        self.started_entered.set()
        await self.release_started.wait()
        self._sequence += 1
        self.order.append(f"ledger:started:{tool.value}")
        handle = ToolExecutionHandle(
            execution_id=execution_id,
            logical_call_id=logical_call_id,
            attempt=attempt,
            correlation=correlation,
            tool=tool,
            started_event_sequence=self._sequence,
            started_at=datetime.now(UTC),
        )
        self.handles.append(handle)
        return handle

    async def append_tool_terminal(
        self,
        handle: ToolExecutionHandle,
        *,
        owner_id: str,
        transition: ToolTransition,
        duration_ms: int,
        error_code: ToolFailureCode | None = None,
        summary: ToolExecutionSummary | None = None,
        execution_lease: ExecutionLease | None = None,
    ) -> ToolExecutionRecord:
        self.order.append(f"ledger:terminal:{transition.value}")
        if self.terminal_error is not None:
            raise self.terminal_error
        self._sequence += 1
        record = ToolExecutionRecord(
            handle=handle,
            transition=transition,
            terminal_event_sequence=self._sequence,
            completed_at=datetime.now(UTC),
            duration_ms=duration_ms,
            error_code=error_code,
            summary=summary,
        )
        self.terminals.append(record)
        return record


class OrderedGateway(FakeMcpGateway):
    def __init__(
        self,
        order: list[str],
        *,
        result: AccessibilityQuestionResult | None = None,
        resolve_error: BaseException | None = None,
    ) -> None:
        super().__init__(result=result, resolve_error=resolve_error)
        self.order = order

    async def answer_facility_question(
        self,
        question: str,
    ) -> AccessibilityQuestionResult:
        self.order.append("gateway:answer_facility_question")
        return await super().answer_facility_question(question)

    async def resolve_station(self, query: str) -> StationResolutionResult:
        self.order.append("gateway:resolve_station")
        return await super().resolve_station(query)

    async def generate_accessibility_brief(
        self,
        origin: str,
        destination: str,
        profile: MobilityProfile,
        *,
        compare_alternatives: bool = False,
    ) -> AccessibilityResult:
        self.order.append("gateway:generate_accessibility_brief")
        return await super().generate_accessibility_brief(
            origin,
            destination,
            profile,
            compare_alternatives=compare_alternatives,
        )


def correlation(thread_id: str, *, attempt: int = 1) -> RunExecutionCorrelation:
    return RunExecutionCorrelation(
        thread_id=thread_id,
        run_id=f"run-{thread_id}",
        request_id=f"request-{attempt}",
        attempt=attempt,
    )


def execution_lease(
    selected: RunExecutionCorrelation,
    *,
    generation: int,
) -> ExecutionLease:
    heartbeat_at = datetime.now(UTC)
    return ExecutionLease(
        job_id=f"job-{generation}",
        owner_id="local-test-owner",
        correlation=selected,
        operation="start",
        payload=StartExecutionPayload(question=QUESTION),
        worker_id="worker-1",
        token=f"token-{generation}",
        generation=generation,
        heartbeat_at=heartbeat_at,
        expires_at=heartbeat_at + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_tool_start_must_commit_before_the_gateway_call(settings: Settings) -> None:
    order: list[str] = []
    gateway = OrderedGateway(order)
    ledger = RecordingRunLedger(order, block_started=True)
    runner = MobilityGraphRunner(gateway, settings, run_ledger=ledger)
    thread_id = "ledger-start-order"

    invocation = asyncio.create_task(
        runner.start(
            QUESTION,
            thread_id=thread_id,
            correlation=correlation(thread_id),
        )
    )
    await asyncio.wait_for(ledger.started_entered.wait(), timeout=1)

    assert gateway.resolve_calls == []
    assert order == []

    ledger.release_started.set()
    await invocation

    assert order[:3] == [
        "ledger:started:resolve_station",
        "gateway:resolve_station",
        "ledger:terminal:completed",
    ]


@pytest.mark.asyncio
async def test_lease_generation_is_recorded_as_recovery_tool_attempt(
    settings: Settings,
) -> None:
    order: list[str] = []
    ledger = RecordingRunLedger(order)
    runner = MobilityGraphRunner(OrderedGateway(order), settings, run_ledger=ledger)
    thread_id = "ledger-recovery-attempt"
    selected = correlation(thread_id)

    await runner.start(
        QUESTION,
        thread_id=thread_id,
        correlation=selected,
        execution_lease=execution_lease(selected, generation=3),
    )

    assert [handle.attempt for handle in ledger.handles] == [3, 3, 3]
    assert all(handle.execution_id.endswith(":attempt:3") for handle in ledger.handles)


@pytest.mark.asyncio
async def test_all_gateway_tools_write_only_allowlisted_success_summaries(
    settings: Settings,
) -> None:
    trip_order: list[str] = []
    trip_gateway = OrderedGateway(trip_order)
    trip_ledger = RecordingRunLedger(trip_order)
    trip_runner = MobilityGraphRunner(trip_gateway, settings, run_ledger=trip_ledger)
    trip_thread = "ledger-trip-summary"

    await trip_runner.start(
        QUESTION,
        thread_id=trip_thread,
        correlation=correlation(trip_thread),
    )

    assert [record.transition for record in trip_ledger.terminals] == [
        ToolTransition.COMPLETED,
        ToolTransition.COMPLETED,
        ToolTransition.COMPLETED,
    ]
    assert isinstance(trip_ledger.terminals[0].summary, ResolveStationToolSummary)
    assert isinstance(trip_ledger.terminals[1].summary, ResolveStationToolSummary)
    assert isinstance(
        trip_ledger.terminals[2].summary,
        GenerateAccessibilityBriefToolSummary,
    )
    assert [handle.attempt for handle in trip_ledger.handles] == [1, 1, 1]
    serialized_trip_ledger = json.dumps(
        [record.model_dump(mode="json") for record in trip_ledger.terminals],
        ensure_ascii=False,
    )
    assert "홍대입구" not in serialized_trip_ledger
    assert "삼성" not in serialized_trip_ledger
    assert QUESTION not in serialized_trip_ledger

    facility_order: list[str] = []
    facility_gateway = OrderedGateway(
        facility_order,
        result=facility_question_result(),
    )
    facility_ledger = RecordingRunLedger(facility_order)
    facility_runner = MobilityGraphRunner(
        facility_gateway,
        settings,
        run_ledger=facility_ledger,
    )
    facility_thread = "ledger-facility-summary"

    await facility_runner.start(
        "강남역 엘리베이터 운행 중이야?",
        thread_id=facility_thread,
        correlation=correlation(facility_thread),
    )

    assert len(facility_ledger.terminals) == 1
    assert isinstance(
        facility_ledger.terminals[0].summary,
        AnswerFacilityQuestionToolSummary,
    )
    serialized_facility_ledger = json.dumps(
        facility_ledger.terminals[0].model_dump(mode="json"),
        ensure_ascii=False,
    )
    assert "강남" not in serialized_facility_ledger
    assert "엘리베이터" not in serialized_facility_ledger


@pytest.mark.asyncio
async def test_timeout_is_recorded_before_the_existing_graph_error_path(
    settings: Settings,
) -> None:
    order: list[str] = []
    gateway = OrderedGateway(order, resolve_error=McpTimeoutError())
    ledger = RecordingRunLedger(order)
    runner = MobilityGraphRunner(gateway, settings, run_ledger=ledger)
    thread_id = "ledger-timeout"

    with pytest.raises(McpTimeoutError):
        await runner.start(
            QUESTION,
            thread_id=thread_id,
            correlation=correlation(thread_id),
        )

    assert order == [
        "ledger:started:resolve_station",
        "gateway:resolve_station",
        "ledger:terminal:timeout",
    ]
    assert len(ledger.terminals) == 1
    assert ledger.terminals[0].transition == ToolTransition.TIMEOUT
    assert ledger.terminals[0].error_code == ToolFailureCode.MCP_TIMEOUT
    assert ledger.terminals[0].summary is None


@pytest.mark.asyncio
async def test_terminal_ledger_failure_prevents_the_tool_result_checkpoint(
    settings: Settings,
) -> None:
    order: list[str] = []
    terminal_error = RuntimeError("terminal ledger unavailable")
    gateway = OrderedGateway(order)
    ledger = RecordingRunLedger(order, terminal_error=terminal_error)
    runner = MobilityGraphRunner(gateway, settings, run_ledger=ledger)
    thread_id = "ledger-terminal-failure"

    with pytest.raises(RuntimeError) as captured:
        await runner.start(
            QUESTION,
            thread_id=thread_id,
            correlation=correlation(thread_id),
        )

    assert captured.value is terminal_error
    assert order == [
        "ledger:started:resolve_station",
        "gateway:resolve_station",
        "ledger:terminal:completed",
    ]
    assert gateway.resolve_calls == ["홍대입구"]
    assert gateway.brief_calls == []
    assert ledger.terminals == []
    state = await runner.get_state(thread_id)
    assert state.get("origin_resolution") is None
    assert state.get("external_error") is None


@pytest.mark.asyncio
async def test_cancellation_is_safely_recorded_and_repropagated(settings: Settings) -> None:
    order: list[str] = []
    gateway = OrderedGateway(order, resolve_error=asyncio.CancelledError())
    ledger = RecordingRunLedger(order)
    runner = MobilityGraphRunner(gateway, settings, run_ledger=ledger)
    thread_id = "ledger-cancelled"

    with pytest.raises(NodeCancelledError) as captured:
        await runner.start(
            QUESTION,
            thread_id=thread_id,
            correlation=correlation(thread_id),
        )

    assert isinstance(captured.value.__cause__, asyncio.CancelledError)
    assert len(ledger.terminals) == 1
    assert ledger.terminals[0].transition == ToolTransition.FAILED
    assert ledger.terminals[0].error_code == ToolFailureCode.EXECUTION_CANCELLED
    assert order == [
        "ledger:started:resolve_station",
        "gateway:resolve_station",
        "ledger:terminal:failed",
    ]
