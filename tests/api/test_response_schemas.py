from __future__ import annotations

import pytest
from pydantic import ValidationError

from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.api.schemas.questions import AnswerQuestionResponse
from barrier_free_mobility_agent.api.schemas.threads import ConversationRunResponse
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.domain.models import (
    ActiveClarificationSummary,
    AnswerEvidence,
    AnswerOutcome,
    AnswerSource,
    ClarificationControl,
    ClarificationField,
    ClarificationOption,
    ConversationContextView,
    QuestionIntent,
    ResponseStatus,
)


def _clarification_outcome() -> AnswerOutcome:
    return AnswerOutcome(
        source=AnswerSource.AGENT,
        status=ResponseStatus.NEEDS_CLARIFICATION,
        intent=QuestionIntent.TRIP_ACCESSIBILITY,
        user_message="출발역 후보를 선택해 주세요.",
        clarification_needed=True,
        questions=["어느 시청역인가요?"],
        clarification_options=[
            ClarificationOption(value="시청 (1호선)", label="시청역 · 1호선"),
            ClarificationOption(value="시청 (2호선)", label="시청역 · 2호선"),
        ],
        evidence=AnswerEvidence(),
    )


def _clarification_context() -> ConversationContextView:
    clarification = _clarification_outcome().clarification
    assert clarification is not None
    return ConversationContextView(
        intent=QuestionIntent.TRIP_ACCESSIBILITY,
        active_clarification=ActiveClarificationSummary(
            id=clarification.id,
            prompt_ids=tuple(prompt.id for prompt in clarification.prompts),
            fields=tuple(
                dict.fromkeys(field for prompt in clarification.prompts for field in prompt.fields)
            ),
        ),
    )


def test_question_response_projects_api_safe_clarification_options() -> None:
    response = AnswerQuestionResponse.from_result(
        request_id="request-1",
        outcome=_clarification_outcome(),
    )

    assert response.model_dump(mode="json")["clarification_options"] == [
        {"value": "시청 (1호선)", "label": "시청역 · 1호선"},
        {"value": "시청 (2호선)", "label": "시청역 · 2호선"},
    ]
    assert response.clarification is not None
    assert response.clarification.control == ClarificationControl.CHOICE
    assert response.clarification.prompts[0].fields == (ClarificationField.OTHER,)
    assert response.schema_version == "3"


def test_thread_response_projects_api_safe_clarification_options() -> None:
    run = ConversationRun(
        run_id="a" * 32,
        thread_id="b" * 32,
        status=GraphTurnStatus.INTERRUPTED,
        resume_mode=ResumeMode.COMMAND,
        outcome=_clarification_outcome(),
        context=_clarification_context(),
    )

    response = ConversationRunResponse.from_run(request_id="request-2", run=run)

    assert response.clarification_options == _clarification_outcome().clarification_options
    assert response.clarification == _clarification_outcome().clarification
    assert response.context.schema_version == "2"
    assert response.schema_version == "3"


def test_choice_options_require_exactly_one_legacy_question() -> None:
    with pytest.raises(ValidationError, match="exactly one question"):
        AnswerOutcome(
            source=AnswerSource.AGENT,
            status=ResponseStatus.NEEDS_CLARIFICATION,
            intent=QuestionIntent.TRIP_ACCESSIBILITY,
            user_message="두 질문 중 하나를 선택해 주세요.",
            clarification_needed=True,
            questions=["첫 질문", "둘째 질문"],
            clarification_options=[ClarificationOption(value="yes", label="예")],
            evidence=AnswerEvidence(),
        )


def test_completed_run_cannot_project_an_active_clarification() -> None:
    run = ConversationRun(
        run_id="a" * 32,
        thread_id="b" * 32,
        status=GraphTurnStatus.COMPLETED,
        resume_mode=ResumeMode.NONE,
        outcome=_clarification_outcome(),
        context=_clarification_context(),
    )

    with pytest.raises(ValidationError, match="Completed run"):
        ConversationRunResponse.from_run(request_id="request-3", run=run)


def test_response_schemas_expose_only_agent_owned_clarification_option() -> None:
    schemas = (
        AnswerQuestionResponse.model_json_schema(),
        ConversationRunResponse.model_json_schema(),
    )

    for schema in schemas:
        component_names = set(schema["$defs"])
        assert "ClarificationOption" in component_names
        assert "ClarificationView" in component_names
        assert "TripAnswerPresentation" in component_names
        assert "FacilityAnswerPresentation" in component_names
        assert "AccessibilityResult" not in component_names
        assert "AccessibilityQuestionResult" not in component_names
        assert "MobilityProfile" not in component_names
        assert "StationResolutionResult" not in component_names
        assert "PendingClarification" not in component_names
        assert "TripContext" not in component_names
