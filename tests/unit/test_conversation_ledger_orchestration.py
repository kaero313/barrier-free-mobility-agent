from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from langgraph.errors import NodeCancelledError
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner
from barrier_free_mobility_agent.agent.models import (
    GraphTurn,
    GraphTurnStatus,
    ResumeMode,
)
from barrier_free_mobility_agent.api.container import ApplicationContainer
from barrier_free_mobility_agent.api.routes import threads as thread_routes
from barrier_free_mobility_agent.api.schemas.threads import (
    ResumeRunRequest,
    StartRunRequest,
)
from barrier_free_mobility_agent.application.conversations import ConversationService
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.clarification import (
    FieldsClarificationAnswer,
    SingleTextClarificationAnswer,
)
from barrier_free_mobility_agent.domain.errors import (
    InvalidQuestionError,
    QuestionTooLongError,
    RunNotFoundError,
    StateInputBudgetExceededError,
)
from barrier_free_mobility_agent.domain.identity import LOCAL_OWNER_ID, OwnerId
from barrier_free_mobility_agent.domain.models import (
    AnswerOutcome,
    ResponseStatus,
    Station,
    StationResolutionResult,
)
from barrier_free_mobility_agent.domain.run_ledger import (
    RunEventRecord,
    RunEventType,
    RunExecutionCorrelation,
    RunExecutionPhase,
)
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository


class InjectedProcessCrash(BaseException):
    pass


class ObservingRunner:
    def __init__(
        self,
        repository: MemoryConversationRepository,
        *,
        start_turn: GraphTurn | None = None,
        resume_turn: GraphTurn | None = None,
    ) -> None:
        self.repository = repository
        self.start_turn = start_turn or _turn(
            GraphTurnStatus.INTERRUPTED,
            ResumeMode.COMMAND,
        )
        self.resume_turn = resume_turn or _turn(
            GraphTurnStatus.COMPLETED,
            ResumeMode.NONE,
        )
        self.start_error: BaseException | None = None
        self.resume_error: BaseException | None = None
        self.start_questions: list[str] = []
        self.start_correlations: list[RunExecutionCorrelation] = []
        self.resume_correlations: list[RunExecutionCorrelation] = []
        self.start_observations: list[tuple[tuple[RunEventType, ...], str | None]] = []
        self.deleted_threads: list[str] = []

    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn:
        assert thread_id is not None
        assert correlation is not None
        self.start_questions.append(question)
        events = await self.repository.get_run_events(
            correlation.run_id,
            owner_id=str(owner_id),
        )
        thread = await self.repository.get_thread(thread_id, owner_id=str(owner_id))
        self.start_correlations.append(correlation)
        self.start_observations.append(
            (tuple(event.event_type for event in events), thread.active_run_id)
        )
        if self.start_error is not None:
            raise self.start_error
        return self.start_turn

    async def resume(
        self,
        answer: str | FieldsClarificationAnswer,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn:
        del answer, thread_id, owner_id
        assert correlation is not None
        self.resume_correlations.append(correlation)
        if self.resume_error is not None:
            raise self.resume_error
        return self.resume_turn

    async def continue_business_clarification(
        self,
        answer: str,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn:
        return await self.resume(
            answer,
            thread_id=thread_id,
            owner_id=owner_id,
            correlation=correlation,
        )

    async def delete_thread(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId = LOCAL_OWNER_ID,
    ) -> None:
        del owner_id
        self.deleted_threads.append(thread_id)


class FailingInitialReservationRepository(MemoryConversationRepository):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.reservation_calls = 0

    async def reserve_initial_run(
        self,
        correlation: RunExecutionCorrelation,
        *,
        owner_id: str,
        capacity: int,
    ) -> RunEventRecord:
        del correlation, owner_id, capacity
        self.reservation_calls += 1
        raise self.error


def _turn(status: GraphTurnStatus, resume_mode: ResumeMode) -> GraphTurn:
    return GraphTurn(
        status=status,
        resume_mode=resume_mode,
        outcome=AnswerOutcome.question_too_complex(),
        state={},
    )


def _node_cancelled_with(cause: BaseException | None) -> NodeCancelledError:
    error = NodeCancelledError("test-node")
    error.__cause__ = cause
    return error


def _service(
    repository: MemoryConversationRepository,
    runner: ObservingRunner,
    *,
    request_max_chars: int = 2_000,
) -> ConversationService:
    return ConversationService(
        runner,
        repository,
        max_threads=4,
        max_runs_per_thread=4,
        request_max_chars=request_max_chars,
        flow_version="ledger-test-flow",
        state_schema_version="ledger-test-state",
    )


@pytest.mark.asyncio
async def test_initial_reservation_and_correlation_exist_before_graph_call() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    service = _service(repository, runner)
    thread_id = await service.create_thread()

    run = await service.start_run(
        thread_id,
        "question",
        request_id="route-start-request",
    )

    assert runner.start_correlations == [
        RunExecutionCorrelation(
            thread_id=thread_id,
            run_id=run.run_id,
            request_id="route-start-request",
            attempt=1,
        )
    ]
    assert runner.start_observations == [((RunEventType.RUN_STARTED,), run.run_id)]
    events = await repository.get_run_events(run.run_id, owner_id=str(LOCAL_OWNER_ID))
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.RUN_INTERRUPTED,
    ]


@pytest.mark.asyncio
async def test_start_input_is_normalized_before_reservation_and_runner_call() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    service = _service(repository, runner, request_max_chars=3)
    thread_id = await service.create_thread()

    run = await service.start_run(
        thread_id,
        "  \N{FULLWIDTH LATIN CAPITAL LETTER A} \n  B  ",
    )

    assert runner.start_questions == ["A B"]
    events = await repository.get_run_events(run.run_id, owner_id=str(LOCAL_OWNER_ID))
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.RUN_INTERRUPTED,
    ]


@pytest.mark.asyncio
async def test_invalid_start_input_never_reserves_run_capacity() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    service = _service(repository, runner, request_max_chars=8)
    thread_id = await service.create_thread()

    with pytest.raises(InvalidQuestionError):
        await service.start_run(thread_id, " \n \t ")
    with pytest.raises(QuestionTooLongError) as raised:
        await service.start_run(thread_id, "123456789")

    assert raised.value.context == {"max_chars": 8, "actual_chars": 9}
    assert runner.start_questions == []
    assert runner.start_correlations == []
    thread = await repository.get_thread(thread_id, owner_id=str(LOCAL_OWNER_ID))
    assert thread.run_count == 0
    assert thread.active_run_id is None


@pytest.mark.asyncio
async def test_route_request_ids_are_preserved_per_start_and_resume_attempt(
    settings: Settings,
) -> None:
    repository = MemoryConversationRepository()
    graph = MobilityGraphRunner(
        FakeMcpGateway(),
        settings,
        run_ledger=repository,
    )
    service = ConversationService(
        graph,
        repository,
        max_threads=4,
        max_runs_per_thread=4,
        request_max_chars=settings.api_request_max_chars,
        flow_version=settings.agent_flow_version,
        state_schema_version=settings.agent_state_schema_version,
    )
    thread_id = await service.create_thread()
    container = cast(
        ApplicationContainer,
        SimpleNamespace(conversations=service),
    )

    started = await thread_routes.start_run(
        thread_id=thread_id,
        payload=StartRunRequest(question="홍대입구역에서 삼성역까지 갈 수 있어?"),
        container=container,
        request_id="route-start-request",
        owner=LOCAL_OWNER_ID,
        response_schema="4",
    )
    assert started.clarification is not None
    completed = await thread_routes.resume_run(
        run_id=started.run_id,
        payload=ResumeRunRequest(
            answer="둘 다 못 써요",
            clarification_id=started.clarification.id,
        ),
        container=container,
        request_id="route-resume-request",
        owner=LOCAL_OWNER_ID,
        response_schema="4",
    )

    assert completed.status != ResponseStatus.NEEDS_CLARIFICATION
    events = await repository.get_run_events(
        started.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    interrupted = next(
        event for event in events if event.event_type == RunEventType.RUN_INTERRUPTED
    )
    resume_started = next(
        event for event in events if event.event_type == RunEventType.RUN_RESUME_STARTED
    )
    assert events[0].correlation.request_id == "route-start-request"
    assert interrupted.correlation.request_id == "route-start-request"
    assert resume_started.correlation.request_id == "route-resume-request"
    assert all(
        event.correlation.request_id == "route-resume-request"
        for event in events
        if event.sequence >= resume_started.sequence
    )
    assert resume_started.correlation.attempt == 2


@pytest.mark.asyncio
async def test_reservation_failure_never_calls_the_runner() -> None:
    reservation_error = RuntimeError("run reservation unavailable")
    repository = FailingInitialReservationRepository(reservation_error)
    runner = ObservingRunner(repository)
    service = _service(repository, runner)
    thread_id = await service.create_thread()

    with pytest.raises(RuntimeError) as captured:
        await service.start_run(thread_id, "question", request_id="request-1")

    assert captured.value is reservation_error
    assert repository.reservation_calls == 1
    assert runner.start_correlations == []
    thread = await repository.get_thread(thread_id, owner_id=str(LOCAL_OWNER_ID))
    assert thread.active_run_id is None
    assert thread.run_count == 0


@pytest.mark.asyncio
async def test_unexpected_start_failure_is_materialized_as_failed_run_and_event() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    start_error = RuntimeError("unexpected graph failure")
    runner.start_error = start_error
    service = _service(repository, runner)
    thread_id = await service.create_thread()

    with pytest.raises(RuntimeError) as captured:
        await service.start_run(thread_id, "question", request_id="request-1")

    assert captured.value is start_error
    correlation = runner.start_correlations[0]
    failed = await repository.get_run(
        correlation.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert failed.status == GraphTurnStatus.FAILED
    assert failed.outcome.status == ResponseStatus.FAILED
    events = await repository.get_run_events(
        correlation.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.RUN_FAILED,
    ]
    assert (
        await repository.find_orphaned_runs(
            owner_id=str(LOCAL_OWNER_ID),
            stale_before=datetime.now(UTC) + timedelta(seconds=1),
        )
        == ()
    )


@pytest.mark.asyncio
async def test_correctable_and_cancelled_resumes_restore_interrupted_with_new_sequences() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    service = _service(repository, runner)
    thread_id = await service.create_thread()
    started = await service.start_run(
        thread_id,
        "question",
        request_id="initial-request",
    )

    runner.resume_error = StateInputBudgetExceededError(
        context={"budget": "state_bytes", "limit": 10, "actual": 11}
    )
    with pytest.raises(StateInputBudgetExceededError):
        await service.resume_run(
            started.run_id,
            "too large",
            request_id="correctable-request",
        )

    runner.resume_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.resume_run(
            started.run_id,
            "cancelled",
            request_id="cancelled-request",
        )

    unchanged = await service.get_run(started.run_id)
    assert unchanged == started
    thread = await repository.get_thread(thread_id, owner_id=str(LOCAL_OWNER_ID))
    assert thread.active_run_id == started.run_id
    events = await repository.get_run_events(
        started.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert [event.sequence for event in events] == list(range(1, 7))
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.RUN_INTERRUPTED,
        RunEventType.RUN_RESUME_STARTED,
        RunEventType.RUN_INTERRUPTED,
        RunEventType.RUN_RESUME_STARTED,
        RunEventType.RUN_INTERRUPTED,
    ]
    assert [correlation.request_id for correlation in runner.resume_correlations] == [
        "correctable-request",
        "cancelled-request",
    ]
    assert [correlation.attempt for correlation in runner.resume_correlations] == [2, 3]


@pytest.mark.asyncio
async def test_invalid_resume_input_is_rejected_without_reservation_events() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    service = _service(repository, runner, request_max_chars=8)
    thread_id = await service.create_thread()
    started = await service.start_run(thread_id, "question")

    with pytest.raises(InvalidQuestionError):
        await service.resume_run(started.run_id, "  \n ", request_id="blank")
    with pytest.raises(QuestionTooLongError):
        await service.resume_run(
            started.run_id,
            SingleTextClarificationAnswer(value="123456789"),
            request_id="oversized",
        )

    assert runner.resume_correlations == []
    assert await service.get_run(started.run_id) == started
    events = await repository.get_run_events(
        started.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.RUN_INTERRUPTED,
    ]


@pytest.mark.asyncio
async def test_wrapped_node_cancellation_restores_interrupted_resume_snapshot() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    service = _service(repository, runner)
    thread_id = await service.create_thread()
    started = await service.start_run(thread_id, "question")
    cancellation = asyncio.CancelledError()
    wrapped = RuntimeError("retry wrapper")
    wrapped.__cause__ = cancellation
    node_cancelled = _node_cancelled_with(wrapped)
    runner.resume_error = node_cancelled

    with pytest.raises(NodeCancelledError) as raised:
        await service.resume_run(
            started.run_id,
            "answer",
            request_id="cancelled-request",
        )

    assert raised.value is node_cancelled
    assert await service.get_run(started.run_id) == started
    events = await repository.get_run_events(
        started.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.RUN_INTERRUPTED,
        RunEventType.RUN_RESUME_STARTED,
        RunEventType.RUN_INTERRUPTED,
    ]

    runner.resume_error = None
    completed = await service.resume_run(started.run_id, "answer")
    assert completed.status == GraphTurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_real_graph_tool_cancellation_keeps_resume_run_interrupted(
    settings: Settings,
) -> None:
    repository = MemoryConversationRepository()
    gateway = FakeMcpGateway(
        station_results={
            "시청": StationResolutionResult(
                query="시청",
                matched_station=None,
                candidates=(
                    Station(station_name="시청", line="1호선"),
                    Station(station_name="시청", line="2호선"),
                ),
                needs_clarification=True,
                clarification_message="시청역 노선을 선택해 주세요.",
            )
        }
    )
    graph = MobilityGraphRunner(gateway, settings, run_ledger=repository)
    service = ConversationService(
        graph,
        repository,
        max_threads=4,
        max_runs_per_thread=4,
        request_max_chars=settings.api_request_max_chars,
        flow_version=settings.agent_flow_version,
        state_schema_version=settings.agent_state_schema_version,
    )
    thread_id = await service.create_thread()
    started = await service.start_run(
        thread_id,
        (
            "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
            "시청역에서 삼성역까지 갈 수 있어?"
        ),
    )
    assert started.status == GraphTurnStatus.INTERRUPTED
    gateway.resolve_error = asyncio.CancelledError()

    with pytest.raises(NodeCancelledError) as raised:
        await service.resume_run(
            started.run_id,
            "시청 (1호선)",
            request_id="real-graph-cancelled-request",
        )

    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    assert await service.get_run(started.run_id) == started
    events = await repository.get_run_events(
        started.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert [event.event_type for event in events[-4:]] == [
        RunEventType.RUN_RESUME_STARTED,
        RunEventType.TOOL_STARTED,
        RunEventType.TOOL_FAILED,
        RunEventType.RUN_INTERRUPTED,
    ]


@pytest.mark.asyncio
async def test_unrelated_node_cancelled_error_remains_terminal() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    service = _service(repository, runner)
    thread_id = await service.create_thread()
    started = await service.start_run(thread_id, "question")
    node_error = _node_cancelled_with(RuntimeError("not a cancellation"))
    runner.resume_error = node_error

    with pytest.raises(NodeCancelledError) as raised:
        await service.resume_run(started.run_id, "answer")

    assert raised.value is node_error
    failed = await service.get_run(started.run_id)
    assert failed.status == GraphTurnStatus.FAILED
    events = await repository.get_run_events(
        started.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert events[-1].event_type == RunEventType.RUN_FAILED


@pytest.mark.asyncio
async def test_wrapped_node_cancellation_during_start_is_terminal() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    node_cancelled = _node_cancelled_with(asyncio.CancelledError())
    runner.start_error = node_cancelled
    service = _service(repository, runner)
    thread_id = await service.create_thread()

    with pytest.raises(NodeCancelledError) as raised:
        await service.start_run(thread_id, "question")

    assert raised.value is node_cancelled
    correlation = runner.start_correlations[0]
    failed = await service.get_run(correlation.run_id)
    assert failed.status == GraphTurnStatus.FAILED
    events = await repository.get_run_events(
        correlation.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.RUN_FAILED,
    ]


@pytest.mark.asyncio
async def test_base_exception_fault_leaves_a_running_orphan() -> None:
    repository = MemoryConversationRepository()
    runner = ObservingRunner(repository)
    runner.start_error = InjectedProcessCrash("worker terminated")
    service = _service(repository, runner)
    thread_id = await service.create_thread()

    with pytest.raises(InjectedProcessCrash):
        await service.start_run(
            thread_id,
            "question",
            request_id="crash-request",
        )

    correlation = runner.start_correlations[0]
    with pytest.raises(RunNotFoundError):
        await repository.get_run(
            correlation.run_id,
            owner_id=str(LOCAL_OWNER_ID),
        )
    thread = await repository.get_thread(thread_id, owner_id=str(LOCAL_OWNER_ID))
    assert thread.active_run_id == correlation.run_id
    events = await repository.get_run_events(
        correlation.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert [event.sequence for event in events] == [1]
    assert events[0].phase == RunExecutionPhase.RUNNING
    orphans = await repository.find_orphaned_runs(
        owner_id=str(LOCAL_OWNER_ID),
        stale_before=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert len(orphans) == 1
    assert orphans[0].correlation == correlation
    assert orphans[0].last_sequence == 1
