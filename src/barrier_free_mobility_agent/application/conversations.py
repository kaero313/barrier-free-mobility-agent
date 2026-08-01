from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import uuid4

from langgraph.errors import NodeCancelledError
from pydantic import BaseModel, ConfigDict, Field

from barrier_free_mobility_agent.agent.models import (
    GraphTurn,
    GraphTurnStatus,
    ResumeMode,
)
from barrier_free_mobility_agent.agent.router import normalize_input
from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationRepository,
    ConversationThread,
    PersistenceKind,
)
from barrier_free_mobility_agent.domain.clarification import (
    FieldsClarificationAnswer,
    SingleTextClarificationAnswer,
    project_structured_clarification,
    validate_structured_answer,
)
from barrier_free_mobility_agent.domain.errors import (
    AgentCapacityExceededError,
    AgentStateIncompatibleError,
    ClarificationAnswerValidationError,
    InvalidQuestionError,
    QuestionTooLongError,
    RunNotResumableError,
    StaleClarificationError,
    StateInputBudgetExceededError,
    ThreadBusyError,
)
from barrier_free_mobility_agent.domain.identity import LOCAL_OWNER_ID, OwnerId
from barrier_free_mobility_agent.domain.models import (
    AnswerEvidence,
    AnswerOutcome,
    AnswerSource,
    ConversationContextView,
    QuestionIntent,
    ResponseStatus,
)
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation
from barrier_free_mobility_agent.domain.usage import RunUsage


class ConversationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    thread_id: str
    status: GraphTurnStatus
    resume_mode: ResumeMode
    outcome: AnswerOutcome
    context: ConversationContextView = Field(default_factory=ConversationContextView)
    usage: RunUsage = Field(default_factory=RunUsage)


class ConversationGraphRunner(Protocol):
    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn: ...

    async def resume(
        self,
        answer: str | FieldsClarificationAnswer,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn: ...

    async def continue_business_clarification(
        self,
        answer: str,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn: ...

    async def delete_thread(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId = LOCAL_OWNER_ID,
    ) -> None: ...


class ConversationService:
    """Coordinates owner-scoped metadata with the LangGraph checkpoint lifecycle."""

    def __init__(
        self,
        runner: ConversationGraphRunner,
        repository: ConversationRepository,
        *,
        max_threads: int,
        max_runs_per_thread: int,
        request_max_chars: int,
        flow_version: str,
        state_schema_version: str,
        max_total_threads: int | None = None,
    ) -> None:
        self._runner = runner
        self._repository = repository
        self._max_threads = max_threads
        self._max_total_threads = max_total_threads or max_threads
        self._max_runs_per_thread = max_runs_per_thread
        self._request_max_chars = request_max_chars
        self._flow_version = flow_version
        self._state_schema_version = state_schema_version
        self._locks_guard = asyncio.Lock()
        self._thread_locks: dict[str, asyncio.Lock] = {}

    @property
    def persistence(self) -> PersistenceKind:
        return self._repository.persistence

    async def is_ready(self) -> bool:
        return await self._repository.is_ready()

    async def create_thread(self, *, owner: OwnerId = LOCAL_OWNER_ID) -> str:
        thread_id = uuid4().hex
        await self._repository.create_thread(
            ConversationThread(
                thread_id=thread_id,
                owner_id=str(owner),
                flow_version=self._flow_version,
                state_schema_version=self._state_schema_version,
            ),
            capacity=self._max_threads,
            total_capacity=self._max_total_threads,
        )
        return thread_id

    async def start_run(
        self,
        thread_id: str,
        question: str,
        *,
        owner: OwnerId = LOCAL_OWNER_ID,
        request_id: str | None = None,
    ) -> ConversationRun:
        await self._repository.get_thread(thread_id, owner_id=str(owner))
        normalized_question = self._normalize_request_text(question)
        lock = await self._lock_for(thread_id)
        async with lock:
            thread = await self._repository.get_thread(thread_id, owner_id=str(owner))
            self._ensure_thread_compatible(thread)
            if thread.active_run_id is not None:
                raise ThreadBusyError(context={"thread_id": thread_id})
            if thread.run_count >= self._max_runs_per_thread:
                raise AgentCapacityExceededError(
                    context={
                        "resource": "runs_per_thread",
                        "limit": self._max_runs_per_thread,
                    }
                )
            correlation = RunExecutionCorrelation(
                thread_id=thread_id,
                run_id=uuid4().hex,
                request_id=request_id or uuid4().hex,
                attempt=1,
            )
            await self._repository.reserve_initial_run(
                correlation,
                owner_id=str(owner),
                capacity=self._max_runs_per_thread,
            )
            try:
                turn = await self._runner.start(
                    normalized_question,
                    thread_id=thread_id,
                    owner_id=owner,
                    correlation=correlation,
                )
            except asyncio.CancelledError:
                await self._repository.finalize_run(
                    failed_initial_run(correlation),
                    correlation=correlation,
                    owner_id=str(owner),
                )
                raise
            except Exception:
                await self._repository.finalize_run(
                    failed_initial_run(correlation),
                    correlation=correlation,
                    owner_id=str(owner),
                )
                raise
            run = ConversationRun(
                run_id=correlation.run_id,
                thread_id=thread_id,
                status=turn.status,
                resume_mode=turn.resume_mode,
                outcome=turn.outcome,
                context=turn.context,
                usage=turn.usage,
            )
            return await self._repository.finalize_run(
                run,
                correlation=correlation,
                owner_id=str(owner),
            )

    async def resume_run(
        self,
        run_id: str,
        answer: str | SingleTextClarificationAnswer | FieldsClarificationAnswer,
        *,
        owner: OwnerId = LOCAL_OWNER_ID,
        clarification_id: str | None = None,
        request_id: str | None = None,
    ) -> ConversationRun:
        current = await self.get_run(run_id, owner=owner)
        lock = await self._lock_for(current.thread_id)
        async with lock:
            current = await self.get_run(run_id, owner=owner)
            thread = await self._repository.get_thread(
                current.thread_id,
                owner_id=str(owner),
            )
            self._ensure_thread_compatible(thread)
            if not _is_resumable(current) or thread.active_run_id != run_id:
                raise RunNotResumableError(context={"run_id": run_id})
            if clarification_id is not None and (
                current.outcome.clarification is None
                or current.outcome.clarification.id != clarification_id
            ):
                raise StaleClarificationError()
            if isinstance(answer, SingleTextClarificationAnswer):
                runner_answer: str | FieldsClarificationAnswer = answer.value
            else:
                runner_answer = answer
            if isinstance(answer, (SingleTextClarificationAnswer, FieldsClarificationAnswer)):
                active_clarification = current.outcome.clarification
                if active_clarification is None:
                    raise RunNotResumableError(context={"run_id": run_id})
                structured_view = project_structured_clarification(active_clarification)
                validate_structured_answer(structured_view, answer)
            if isinstance(runner_answer, str):
                runner_answer = self._normalize_request_text(runner_answer)
            new_turn_answer: str | None = None
            if current.resume_mode == ResumeMode.NEW_TURN:
                if not isinstance(runner_answer, str):
                    raise ClarificationAnswerValidationError(
                        issues=(
                            {
                                "type": "clarification_answer_type_mismatch",
                                "loc": ("body", "answer", "mode"),
                                "msg": "현재 추가 질문에는 하나의 텍스트 답변이 필요합니다.",
                            },
                        )
                    )
                new_turn_answer = runner_answer
            reservation = await self._repository.reserve_resume_attempt(
                run_id,
                request_id=request_id or uuid4().hex,
                owner_id=str(owner),
            )
            correlation = reservation.correlation
            try:
                if current.resume_mode == ResumeMode.COMMAND:
                    turn = await self._runner.resume(
                        runner_answer,
                        thread_id=current.thread_id,
                        owner_id=owner,
                        correlation=correlation,
                    )
                elif current.resume_mode == ResumeMode.NEW_TURN:
                    assert new_turn_answer is not None
                    turn = await self._runner.continue_business_clarification(
                        new_turn_answer,
                        thread_id=current.thread_id,
                        owner_id=owner,
                        correlation=correlation,
                    )
                else:
                    raise RunNotResumableError(context={"run_id": run_id})
            except asyncio.CancelledError:
                await self._repository.finalize_run(
                    current,
                    correlation=correlation,
                    owner_id=str(owner),
                )
                raise
            except (
                InvalidQuestionError,
                QuestionTooLongError,
                StateInputBudgetExceededError,
                ClarificationAnswerValidationError,
            ):
                # The client can correct request input without invalidating the checkpoint.
                await self._repository.finalize_run(
                    current,
                    correlation=correlation,
                    owner_id=str(owner),
                )
                raise
            except Exception as error:
                if isinstance(error, NodeCancelledError) and _wraps_asyncio_cancellation(error):
                    await self._repository.finalize_run(
                        current,
                        correlation=correlation,
                        owner_id=str(owner),
                    )
                    raise
                failed = failed_resume_run(current)
                await self._repository.finalize_run(
                    failed,
                    correlation=correlation,
                    owner_id=str(owner),
                )
                raise
            updated = current.model_copy(
                update={
                    "status": turn.status,
                    "resume_mode": turn.resume_mode,
                    "outcome": turn.outcome,
                    "context": turn.context,
                    "usage": turn.usage,
                }
            )
            return await self._repository.finalize_run(
                updated,
                correlation=correlation,
                owner_id=str(owner),
            )

    async def get_run(
        self,
        run_id: str,
        *,
        owner: OwnerId = LOCAL_OWNER_ID,
    ) -> ConversationRun:
        return await self._repository.get_run(run_id, owner_id=str(owner))

    async def delete_thread(
        self,
        thread_id: str,
        *,
        owner: OwnerId = LOCAL_OWNER_ID,
    ) -> None:
        await self._repository.get_thread(thread_id, owner_id=str(owner))
        lock = await self._lock_for(thread_id)
        async with lock:
            await self._repository.get_thread(thread_id, owner_id=str(owner))
            deletion = await self._repository.prepare_thread_deletion(
                thread_id,
                owner_id=str(owner),
            )
            await self._runner.delete_thread(thread_id, owner_id=owner)
            await self._repository.commit_thread_deletion(deletion)
        async with self._locks_guard:
            if self._thread_locks.get(thread_id) is lock:
                self._thread_locks.pop(thread_id, None)

    async def _lock_for(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._thread_locks.setdefault(thread_id, asyncio.Lock())

    def _normalize_request_text(self, value: str) -> str:
        normalized = normalize_input(value)
        if not normalized:
            raise InvalidQuestionError()
        if len(normalized) > self._request_max_chars:
            raise QuestionTooLongError(
                context={
                    "max_chars": self._request_max_chars,
                    "actual_chars": len(normalized),
                }
            )
        return normalized

    def _ensure_thread_compatible(self, thread: ConversationThread) -> None:
        if (
            thread.flow_version != self._flow_version
            or thread.state_schema_version != self._state_schema_version
        ):
            raise AgentStateIncompatibleError(
                context={
                    "expected_schema_version": self._state_schema_version,
                    "expected_flow_version": self._flow_version,
                }
            )


def _is_resumable(run: ConversationRun) -> bool:
    return run.status == GraphTurnStatus.INTERRUPTED and run.resume_mode != ResumeMode.NONE


def failed_initial_run(correlation: RunExecutionCorrelation) -> ConversationRun:
    return ConversationRun(
        run_id=correlation.run_id,
        thread_id=correlation.thread_id,
        status=GraphTurnStatus.FAILED,
        resume_mode=ResumeMode.NONE,
        outcome=AnswerOutcome(
            source=AnswerSource.AGENT,
            status=ResponseStatus.FAILED,
            intent=QuestionIntent.UNKNOWN,
            user_message="Agent 실행이 완료되지 않았습니다. 새 run으로 다시 시도해 주세요.",
            evidence=AnswerEvidence(),
        ),
    )


def failed_resume_run(current: ConversationRun) -> ConversationRun:
    return current.model_copy(
        update={
            "status": GraphTurnStatus.FAILED,
            "resume_mode": ResumeMode.NONE,
            "outcome": AnswerOutcome(
                source=AnswerSource.AGENT,
                status=ResponseStatus.FAILED,
                intent=current.outcome.intent,
                user_message=("Agent 실행이 완료되지 않았습니다. 새 run으로 다시 시도해 주세요."),
                evidence=AnswerEvidence(),
            ),
            "context": current.context.model_copy(update={"active_clarification": None}),
        }
    )


def _wraps_asyncio_cancellation(error: NodeCancelledError) -> bool:
    cause: BaseException | None = error.__cause__
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, asyncio.CancelledError):
            return True
        seen.add(id(cause))
        cause = cause.__cause__
    return False
