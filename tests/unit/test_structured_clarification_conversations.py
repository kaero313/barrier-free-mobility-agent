from __future__ import annotations

import pytest

from barrier_free_mobility_agent.agent.models import (
    ClarificationKind,
    GraphTurn,
    GraphTurnStatus,
    PendingClarification,
    PendingPrompt,
    ResumeMode,
)
from barrier_free_mobility_agent.agent.projections import project_pending_clarification
from barrier_free_mobility_agent.application.conversations import ConversationService
from barrier_free_mobility_agent.domain.clarification import (
    ClarificationFieldAnswer,
    FieldsClarificationAnswer,
    TextClarificationAnswerValue,
)
from barrier_free_mobility_agent.domain.errors import (
    ClarificationAnswerValidationError,
    StaleClarificationError,
)
from barrier_free_mobility_agent.domain.identity import LOCAL_OWNER_ID, OwnerId
from barrier_free_mobility_agent.domain.models import (
    AnswerEvidence,
    AnswerOutcome,
    AnswerSource,
    QuestionIntent,
    ResponseStatus,
)
from barrier_free_mobility_agent.domain.run_ledger import (
    RunEventType,
    RunExecutionCorrelation,
)
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository


class RecordingStructuredRunner:
    def __init__(self, start_turn: GraphTurn) -> None:
        self.start_turn = start_turn
        self.resume_calls: list[object] = []

    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn:
        del question, thread_id, owner_id
        return self.start_turn

    async def resume(
        self,
        answer: object,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn:
        del thread_id, owner_id
        self.resume_calls.append(answer)
        return self.start_turn

    async def continue_business_clarification(
        self,
        answer: str,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
    ) -> GraphTurn:
        del answer, thread_id, owner_id
        return self.start_turn

    async def delete_thread(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId = LOCAL_OWNER_ID,
    ) -> None:
        del thread_id, owner_id


def _interrupted_required_turn() -> GraphTurn:
    pending = PendingClarification(
        kind=ClarificationKind.REQUIRED_FIELDS,
        intent="trip_accessibility",
        fields=("origin", "destination", "can_use_stairs", "can_use_escalator"),
        questions=(
            "출발역은 어디인가요?",
            "도착역은 어디인가요?",
            "계단과 에스컬레이터를 각각 이용할 수 있나요?",
        ),
        prompts=(
            PendingPrompt(id="origin", question="출발역은 어디인가요?", fields=("origin",)),
            PendingPrompt(
                id="destination",
                question="도착역은 어디인가요?",
                fields=("destination",),
            ),
            PendingPrompt(
                id="mobility",
                question="계단과 에스컬레이터를 각각 이용할 수 있나요?",
                fields=("can_use_stairs", "can_use_escalator"),
            ),
        ),
        message="필수 조건을 확인해 주세요.",
    )
    clarification = project_pending_clarification(
        pending,
        clarification_id="current-clarification",
    )
    return GraphTurn(
        status=GraphTurnStatus.INTERRUPTED,
        resume_mode=ResumeMode.COMMAND,
        outcome=AnswerOutcome(
            source=AnswerSource.AGENT,
            status=ResponseStatus.NEEDS_CLARIFICATION,
            intent=QuestionIntent.TRIP_ACCESSIBILITY,
            user_message=pending.message,
            clarification_needed=True,
            clarification=clarification,
            questions=list(pending.questions),
            evidence=AnswerEvidence(),
        ),
        state={"pending_clarification": pending.model_dump(mode="json")},
    )


def _invalid_missing_answer() -> FieldsClarificationAnswer:
    return FieldsClarificationAnswer(
        values=(
            ClarificationFieldAnswer(
                input_id="origin",
                value=TextClarificationAnswerValue(value="홍대입구"),
            ),
        )
    )


@pytest.mark.asyncio
async def test_stale_id_precedes_semantic_validation_and_invalid_answer_is_side_effect_free() -> (
    None
):
    runner = RecordingStructuredRunner(_interrupted_required_turn())
    repository = MemoryConversationRepository()
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
    started = await service.start_run(thread_id, "question")

    with pytest.raises(StaleClarificationError):
        await service.resume_run(
            started.run_id,
            _invalid_missing_answer(),
            clarification_id="stale-clarification",
        )

    assert runner.resume_calls == []
    assert await service.get_run(started.run_id) == started

    with pytest.raises(ClarificationAnswerValidationError) as raised:
        await service.resume_run(
            started.run_id,
            _invalid_missing_answer(),
            clarification_id="current-clarification",
        )

    assert {issue["type"] for issue in raised.value.issues} == {"clarification_answer_missing"}
    assert runner.resume_calls == []
    assert await service.get_run(started.run_id) == started
    events = await repository.get_run_events(
        started.run_id,
        owner_id=str(LOCAL_OWNER_ID),
    )
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED,
        RunEventType.RUN_INTERRUPTED,
    ]
