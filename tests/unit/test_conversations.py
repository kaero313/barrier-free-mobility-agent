from __future__ import annotations

import asyncio

import pytest

from barrier_free_mobility_agent.agent.models import (
    GraphTurn,
    GraphTurnStatus,
    ResumeMode,
)
from barrier_free_mobility_agent.application.conversations import ConversationService
from barrier_free_mobility_agent.domain.clarification import FieldsClarificationAnswer
from barrier_free_mobility_agent.domain.errors import (
    RunNotResumableError,
    StaleClarificationError,
    StateInputBudgetExceededError,
    ThreadBusyError,
    ThreadNotFoundError,
)
from barrier_free_mobility_agent.domain.identity import LOCAL_OWNER_ID, OwnerId
from barrier_free_mobility_agent.domain.models import AnswerOutcome
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation
from barrier_free_mobility_agent.domain.usage import RunUsage, UsageSource
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository


class FakeConversationRunner:
    def __init__(
        self,
        *,
        start_turn: GraphTurn,
        command_turn: GraphTurn,
        new_turn: GraphTurn,
    ) -> None:
        self.start_turn = start_turn
        self.command_turn = command_turn
        self.new_turn = new_turn
        self.command_error: BaseException | None = None
        self.new_turn_error: BaseException | None = None
        self.start_calls: list[tuple[str, str | None]] = []
        self.command_calls: list[tuple[str | FieldsClarificationAnswer, str]] = []
        self.new_turn_calls: list[tuple[str, str]] = []
        self.deleted_threads: list[str] = []
        self.delete_error: BaseException | None = None

    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn:
        self.start_calls.append((question, thread_id))
        return self.start_turn

    async def resume(
        self,
        answer: str | FieldsClarificationAnswer,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn:
        self.command_calls.append((answer, thread_id))
        if self.command_error is not None:
            raise self.command_error
        return self.command_turn

    async def continue_business_clarification(
        self,
        answer: str,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn:
        self.new_turn_calls.append((answer, thread_id))
        if self.new_turn_error is not None:
            raise self.new_turn_error
        return self.new_turn

    async def delete_thread(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId = LOCAL_OWNER_ID,
    ) -> None:
        self.deleted_threads.append(thread_id)
        if self.delete_error is not None:
            raise self.delete_error


def turn(
    status: GraphTurnStatus,
    resume_mode: ResumeMode,
    *,
    usage: RunUsage | None = None,
) -> GraphTurn:
    return GraphTurn(
        status=status,
        resume_mode=resume_mode,
        outcome=AnswerOutcome.question_too_complex(),
        state={},
        usage=usage or RunUsage(),
    )


def service_for(
    resume_mode: ResumeMode,
) -> tuple[ConversationService, FakeConversationRunner]:
    runner = FakeConversationRunner(
        start_turn=turn(GraphTurnStatus.INTERRUPTED, resume_mode),
        command_turn=turn(GraphTurnStatus.COMPLETED, ResumeMode.NONE),
        new_turn=turn(GraphTurnStatus.COMPLETED, ResumeMode.NONE),
    )
    return (
        ConversationService(
            runner,
            MemoryConversationRepository(),
            max_threads=4,
            max_runs_per_thread=4,
            request_max_chars=2_000,
            flow_version="test-flow",
            state_schema_version="test-state",
        ),
        runner,
    )


@pytest.mark.asyncio
async def test_checkpoint_delete_failure_keeps_durable_deletion_fence_for_retry() -> None:
    repository = MemoryConversationRepository()
    runner = FakeConversationRunner(
        start_turn=turn(GraphTurnStatus.COMPLETED, ResumeMode.NONE),
        command_turn=turn(GraphTurnStatus.COMPLETED, ResumeMode.NONE),
        new_turn=turn(GraphTurnStatus.COMPLETED, ResumeMode.NONE),
    )
    service = ConversationService(
        runner,
        repository,
        max_threads=4,
        max_runs_per_thread=4,
        request_max_chars=2_000,
        flow_version="test-flow",
        state_schema_version="test-state",
    )
    thread_id = await service.create_thread()
    runner.delete_error = RuntimeError("checkpoint cleanup failed")

    with pytest.raises(RuntimeError, match="checkpoint cleanup failed"):
        await service.delete_thread(thread_id)
    replayed = await repository.prepare_thread_deletion(
        thread_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert replayed.replayed is True
    with pytest.raises(ThreadBusyError):
        await service.start_run(thread_id, "blocked while deletion is pending")

    runner.delete_error = None
    await service.delete_thread(thread_id)
    with pytest.raises(ThreadNotFoundError):
        await repository.get_thread(thread_id, owner_id=str(LOCAL_OWNER_ID))
    assert runner.deleted_threads == [thread_id, thread_id]


@pytest.mark.asyncio
async def test_command_resume_mode_uses_langgraph_resume() -> None:
    service, runner = service_for(ResumeMode.COMMAND)
    thread_id = await service.create_thread()
    started = await service.start_run(thread_id, "question")

    resumed = await service.resume_run(started.run_id, "answer")

    assert runner.command_calls == [("answer", thread_id)]
    assert runner.new_turn_calls == []
    assert resumed.status == GraphTurnStatus.COMPLETED
    assert resumed.resume_mode == ResumeMode.NONE


@pytest.mark.asyncio
async def test_state_input_budget_error_keeps_interrupted_run_resumable() -> None:
    service, runner = service_for(ResumeMode.NEW_TURN)
    thread_id = await service.create_thread()
    started = await service.start_run(thread_id, "question")
    runner.new_turn_error = StateInputBudgetExceededError(
        context={"budget": "state_bytes", "limit": 1024, "actual": 1025}
    )

    with pytest.raises(StateInputBudgetExceededError):
        await service.resume_run(started.run_id, "oversized answer")

    unchanged = await service.get_run(started.run_id)
    assert unchanged == started

    runner.new_turn_error = None
    completed = await service.resume_run(started.run_id, "short answer")
    assert completed.status == GraphTurnStatus.COMPLETED
    assert runner.new_turn_calls == [
        ("oversized answer", thread_id),
        ("short answer", thread_id),
    ]


@pytest.mark.asyncio
async def test_new_turn_resume_mode_continues_business_clarification() -> None:
    service, runner = service_for(ResumeMode.NEW_TURN)
    thread_id = await service.create_thread()
    started = await service.start_run(thread_id, "question")

    resumed = await service.resume_run(started.run_id, "additional details")

    assert runner.command_calls == []
    assert runner.new_turn_calls == [("additional details", thread_id)]
    assert resumed.status == GraphTurnStatus.COMPLETED
    assert resumed.resume_mode == ResumeMode.NONE


@pytest.mark.asyncio
async def test_graph_usage_is_projected_to_run_and_accumulates_on_resume() -> None:
    service, runner = service_for(ResumeMode.COMMAND)
    started_usage = _usage(revision=1, input_tokens=100)
    resumed_usage = _usage(revision=2, input_tokens=180)
    runner.start_turn = turn(
        GraphTurnStatus.INTERRUPTED,
        ResumeMode.COMMAND,
        usage=started_usage,
    )
    runner.command_turn = turn(
        GraphTurnStatus.COMPLETED,
        ResumeMode.NONE,
        usage=resumed_usage,
    )
    thread_id = await service.create_thread()

    started = await service.start_run(thread_id, "question")
    resumed = await service.resume_run(started.run_id, "answer")

    assert started.usage == started_usage
    assert resumed.usage == resumed_usage


@pytest.mark.asyncio
async def test_stale_clarification_id_is_rejected_before_graph_resume() -> None:
    service, runner = service_for(ResumeMode.COMMAND)
    thread_id = await service.create_thread()
    started = await service.start_run(thread_id, "question")
    assert started.outcome.clarification is not None

    with pytest.raises(StaleClarificationError):
        await service.resume_run(
            started.run_id,
            "answer",
            clarification_id="older-screen",
        )

    assert runner.command_calls == []
    unchanged = await service.get_run(started.run_id)
    assert unchanged == started

    resumed = await service.resume_run(
        started.run_id,
        "answer",
        clarification_id=started.outcome.clarification.id,
    )
    assert resumed.status == GraphTurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_resume_failure_is_terminal_and_releases_thread() -> None:
    service, runner = service_for(ResumeMode.COMMAND)
    started_usage = _usage(revision=1, input_tokens=100)
    runner.start_turn = turn(
        GraphTurnStatus.INTERRUPTED,
        ResumeMode.COMMAND,
        usage=started_usage,
    )
    thread_id = await service.create_thread()
    started = await service.start_run(thread_id, "question")
    original = RuntimeError("resume failed")
    runner.command_error = original

    with pytest.raises(RuntimeError) as raised:
        await service.resume_run(started.run_id, "answer")

    assert raised.value is original
    failed = await service.get_run(started.run_id)
    assert failed.status == GraphTurnStatus.FAILED
    assert failed.resume_mode == ResumeMode.NONE
    assert failed.usage == started_usage
    with pytest.raises(RunNotResumableError):
        await service.resume_run(started.run_id, "retry")

    runner.command_error = None
    replacement = await service.start_run(thread_id, "new question")
    assert replacement.run_id != started.run_id


@pytest.mark.asyncio
async def test_cancelled_resume_remains_resumable() -> None:
    service, runner = service_for(ResumeMode.COMMAND)
    thread_id = await service.create_thread()
    started = await service.start_run(thread_id, "question")
    runner.command_error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await service.resume_run(started.run_id, "answer")

    unchanged = await service.get_run(started.run_id)
    assert unchanged.status == GraphTurnStatus.INTERRUPTED
    assert unchanged.resume_mode == ResumeMode.COMMAND

    runner.command_error = None
    retried = await service.resume_run(started.run_id, "answer")
    assert retried.status == GraphTurnStatus.COMPLETED


def _usage(*, revision: int, input_tokens: int) -> RunUsage:
    return RunUsage(
        revision=revision,
        model_calls=revision,
        input_tokens=input_tokens,
        output_tokens=0,
        total_tokens=input_tokens,
        source=UsageSource.REPORTED,
    )
