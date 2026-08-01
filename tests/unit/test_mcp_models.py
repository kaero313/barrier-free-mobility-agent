from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.factories import (
    CANONICAL_MESSAGE,
    clarification_result,
    facility_question_result,
    trip_question_result,
)

from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    AnswerEvidence,
    AnswerOutcome,
    AnswerSource,
    QuestionIntent,
    ResponseStatus,
    RiskLevel,
)


def test_trip_response_preserves_canonical_unicode_and_newline() -> None:
    result = trip_question_result()
    outcome = AnswerOutcome.from_mcp_result(result)

    assert result.user_message == CANONICAL_MESSAGE
    assert outcome.user_message == CANONICAL_MESSAGE
    assert "\n" in outcome.user_message
    assert outcome.evidence.risk_level == RiskLevel.CAUTION


def test_partial_unknown_result_is_preserved() -> None:
    result = trip_question_result(
        status=ResponseStatus.PARTIAL,
        risk_level=RiskLevel.UNKNOWN,
    )
    outcome = AnswerOutcome.from_mcp_result(result)

    assert outcome.status == ResponseStatus.PARTIAL
    assert outcome.evidence.risk_level == RiskLevel.UNKNOWN
    assert outcome.evidence.failed_sources[0].source_name == "facility_information"
    assert outcome.evidence.failed_sources[0].display_name == "역 시설 정보"
    assert outcome.evidence.limitations


def test_facility_response_uses_typed_facility_items() -> None:
    result = facility_question_result()

    assert result.facility_result is not None
    item = result.facility_result.items[0]
    assert item.facility_type == "ELEVATOR"
    assert item.facilities[0].status == "AVAILABLE"


def test_clarification_response_requires_questions() -> None:
    payload = clarification_result().model_dump(mode="json")
    payload["questions"] = []

    with pytest.raises(ValidationError, match="Clarification responses require"):
        AccessibilityQuestionResult.model_validate(payload)


def test_trip_and_facility_results_cannot_coexist() -> None:
    payload = trip_question_result().model_dump(mode="json")
    payload["facility_result"] = facility_question_result().model_dump(mode="json")[
        "facility_result"
    ]

    with pytest.raises(ValidationError, match="cannot both be present"):
        AccessibilityQuestionResult.model_validate(payload)


def test_nested_status_must_match_top_level_status() -> None:
    payload = trip_question_result().model_dump(mode="json")
    payload["result"]["status"] = "PARTIAL"

    with pytest.raises(ValidationError, match="must match"):
        AccessibilityQuestionResult.model_validate(payload)


def test_canonical_messages_must_match_exactly() -> None:
    payload = trip_question_result().model_dump(mode="json")
    payload["user_message"] = "다르게 작성된 문장"

    with pytest.raises(ValidationError, match="must match exactly"):
        AccessibilityQuestionResult.model_validate(payload)


def test_additive_mcp_fields_are_not_echoed() -> None:
    result = trip_question_result()
    dumped = result.model_dump(mode="json")

    assert "future_top_level_field" not in dumped
    assert "future_additive_field" not in dumped["result"]


def test_facility_child_type_must_match_item_type() -> None:
    payload = facility_question_result().model_dump(mode="json")
    payload["facility_result"]["items"][0]["facilities"][0]["facility_type"] = "ACCESSIBLE_RESTROOM"

    with pytest.raises(ValidationError, match="child facility types must match"):
        AccessibilityQuestionResult.model_validate(payload)


def test_facility_answer_state_must_match_detail_statuses() -> None:
    payload = facility_question_result().model_dump(mode="json")
    payload["facility_result"]["items"][0]["facilities"][0]["status"] = "MAINTENANCE"

    with pytest.raises(ValidationError, match="answer_state must match"):
        AccessibilityQuestionResult.model_validate(payload)


def test_failed_facility_response_still_requires_structured_result() -> None:
    payload = facility_question_result().model_dump(mode="json")
    payload["status"] = "FAILED"
    payload["facility_result"] = None

    with pytest.raises(ValidationError, match="requires a facility result"):
        AccessibilityQuestionResult.model_validate(payload)


def test_terminal_response_cannot_claim_clarification() -> None:
    payload = facility_question_result().model_dump(mode="json")
    payload["clarification_needed"] = True
    payload["questions"] = ["추가 질문"]

    with pytest.raises(ValidationError, match="clarification_needed must match"):
        AccessibilityQuestionResult.model_validate(payload)


def test_terminal_facility_result_requires_at_least_one_item() -> None:
    payload = facility_question_result().model_dump(mode="json")
    payload["facility_result"]["items"] = []

    with pytest.raises(ValidationError, match="at least one item"):
        AccessibilityQuestionResult.model_validate(payload)


def test_answer_presentation_kind_must_match_intent() -> None:
    facility_outcome = AnswerOutcome.from_mcp_result(facility_question_result())
    assert facility_outcome.presentation is not None

    with pytest.raises(ValidationError, match="Trip outcome requires a trip presentation"):
        AnswerOutcome(
            source=AnswerSource.MCP,
            status=ResponseStatus.SUCCESS,
            intent=QuestionIntent.TRIP_ACCESSIBILITY,
            user_message="잘못 연결된 결과",
            evidence=AnswerEvidence(),
            presentation=facility_outcome.presentation,
        )
