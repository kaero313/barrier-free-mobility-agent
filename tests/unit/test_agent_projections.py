from __future__ import annotations

from datetime import UTC, datetime

from tests.factories import facility_question_result, trip_question_result

from barrier_free_mobility_agent.agent.models import (
    ClarificationKind,
    FacilityContext,
    PendingClarification,
    PendingPrompt,
    TripContext,
)
from barrier_free_mobility_agent.agent.projections import (
    project_conversation_context,
    project_pending_clarification,
)
from barrier_free_mobility_agent.domain.mobility import (
    MobilityContext,
    MobilityFlag,
    SlotSource,
)
from barrier_free_mobility_agent.domain.models import (
    AccessibleFacility,
    ClarificationControl,
    ClarificationField,
    ContextLocationStatus,
    ContextProvenance,
    EvidenceSource,
    FacilityAnswerState,
    FacilityStatus,
    FacilityType,
    QuestionIntent,
    facility_answer_presentation,
    trip_answer_presentation,
)


def test_required_fields_are_one_free_text_turn_with_structured_prompts() -> None:
    pending = PendingClarification(
        kind=ClarificationKind.REQUIRED_FIELDS,
        fields=("origin", "destination", "can_use_stairs", "can_use_escalator"),
        questions=(
            "출발역은 어디인가요?",
            "도착역은 어디인가요?",
            "계단과 에스컬레이터를 각각 이용할 수 있나요?",
        ),
        prompts=(
            PendingPrompt(
                id="origin",
                question="출발역은 어디인가요?",
                fields=("origin",),
            ),
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
        message="정보가 더 필요합니다.",
    )

    view = project_pending_clarification(pending, clarification_id="interrupt-1")

    assert view.control == ClarificationControl.FREE_TEXT
    assert view.answer_mode == "single_text"
    assert [prompt.fields for prompt in view.prompts] == [
        (ClarificationField.ORIGIN,),
        (ClarificationField.DESTINATION,),
        (ClarificationField.CAN_USE_STAIRS, ClarificationField.CAN_USE_ESCALATOR),
    ]
    assert view.options == ()


def test_station_choice_exposes_only_safe_labels() -> None:
    pending = PendingClarification(
        kind=ClarificationKind.STATION_SELECTION,
        fields=("origin",),
        questions=("시청역 노선을 선택해 주세요.",),
        prompts=(
            PendingPrompt(
                id="origin",
                question="시청역 노선을 선택해 주세요.",
                fields=("origin",),
            ),
        ),
        message="시청역을 확인해 주세요.",
        candidates=(
            {"station_name": "시청", "line": "1호선", "label": "시청 (1호선)"},
            {"station_name": "시청", "line": "2호선", "label": "시청 (2호선)"},
        ),
    )

    view = project_pending_clarification(pending, clarification_id="interrupt-2")
    payload = view.model_dump(mode="json")

    assert view.control == ClarificationControl.CHOICE
    assert payload["options"] == [
        {"value": "시청 (1호선)", "label": "시청 (1호선)"},
        {"value": "시청 (2호선)", "label": "시청 (2호선)"},
    ]
    assert "station_name" not in str(payload["options"])
    assert "line" not in str(payload["options"])


def test_context_uses_resolved_locations_and_confirmed_mobility_only() -> None:
    trip = TripContext(
        origin_query="시청",
        destination_query="삼성",
        origin_station="시청",
        destination_station="삼성",
        origin_line="1호선",
        destination_line="2호선",
    )
    mobility = MobilityContext(
        wheelchair=MobilityFlag.known(True, SlotSource.USER_EXPLICIT),
        can_use_stairs=MobilityFlag.known(False, SlotSource.USER_CONFIRMED),
        can_use_escalator=MobilityFlag.known(False, SlotSource.DERIVED_CONSERVATIVE),
    )
    state = {
        "trip_context": trip.model_dump(mode="json"),
        "mobility_context": mobility.model_dump(mode="json"),
        "current_input": "민감한 원문",
        "mcp_profile": {"can_use_escalator": False},
        "tool_ledger": ["private-tool"],
    }

    context = project_conversation_context(
        state,
        intent=QuestionIntent.TRIP_ACCESSIBILITY,
        active_clarification=None,
    )
    payload = context.model_dump(mode="json")

    assert context.origin is not None
    assert context.origin.status == ContextLocationStatus.RESOLVED
    assert context.origin.label == "시청 · 1호선"
    assert context.destination is not None
    assert context.destination.label == "삼성 · 2호선"
    projected_conditions = [
        (condition.field, condition.value, condition.provenance)
        for condition in context.mobility_conditions
    ]
    assert projected_conditions == [
        (
            ClarificationField.WHEELCHAIR,
            True,
            ContextProvenance.USER_PROVIDED,
        ),
        (
            ClarificationField.CAN_USE_STAIRS,
            False,
            ContextProvenance.USER_CONFIRMED,
        ),
    ]
    serialized = str(payload)
    assert "민감한 원문" not in serialized
    assert "private-tool" not in serialized
    assert "mcp_profile" not in serialized


def test_presentation_allowlists_public_evidence_summary() -> None:
    result = trip_question_result().result
    assert result is not None
    checked_at = datetime(2026, 7, 10, 3, 0, tzinfo=UTC)
    result = result.model_copy(
        update={
            "evidence_sources": [
                EvidenceSource(
                    source_name="public-station-api",
                    display_name="도시철도 시설 정보",
                    source_type="public_api",
                    checked_at=checked_at,
                    note="공개하면 안 되는 원본 메모",
                ),
                EvidenceSource(
                    source_name="internal-cache-key",
                    display_name="내부 운영 데이터",
                    source_type="internal",
                    checked_at=checked_at,
                ),
                EvidenceSource(
                    source_name="fixture-name",
                    display_name="테스트 fixture",
                    source_type="fixture",
                    checked_at=checked_at,
                ),
            ]
        }
    )

    presentation = trip_answer_presentation(result)
    payload = presentation.model_dump(mode="json")

    assert presentation.trip.route_summary == result.route_summary
    assert payload["evidence_sources"] == [
        {
            "display_name": "도시철도 시설 정보",
            "source_type": "public_api",
            "checked_at": "2026-07-10T03:00:00Z",
            "success": True,
        }
    ]
    serialized = str(payload)
    assert "public-station-api" not in serialized
    assert "internal-cache-key" not in serialized
    assert "fixture-name" not in serialized
    assert "원본 메모" not in serialized


def test_facility_context_hides_retained_trip_state() -> None:
    state = {
        "trip_context": TripContext(
            origin_query="홍대입구",
            destination_query="삼성",
            origin_station="홍대입구",
            destination_station="삼성",
        ).model_dump(mode="json"),
        "mobility_context": MobilityContext(
            wheelchair=MobilityFlag.known(True, SlotSource.USER_EXPLICIT)
        ).model_dump(mode="json"),
        "facility_context": FacilityContext(
            target_query="강남",
            target_station="강남",
            target_line="2",
            target_source=SlotSource.USER_EXPLICIT,
            facility_types=(FacilityType.ELEVATOR,),
            facility_types_source=SlotSource.USER_EXPLICIT,
        ).model_dump(mode="json"),
    }

    context = project_conversation_context(
        state,
        intent=QuestionIntent.FACILITY_STATUS,
        active_clarification=None,
    )

    assert context.origin is None
    assert context.destination is None
    assert context.mobility_conditions == ()
    assert context.facility is not None
    assert context.facility.target is not None
    assert context.facility.target.label == "강남 · 2호선"
    assert context.facility.target_provenance == ContextProvenance.USER_PROVIDED


def test_facility_presentation_caps_details_and_removes_provider_only_fields() -> None:
    result = facility_question_result().facility_result
    assert result is not None
    facilities = [
        AccessibleFacility(
            facility_id=f"private-{index}",
            station_name="강남",
            line="2호선",
            facility_type=FacilityType.ELEVATOR,
            status=FacilityStatus.AVAILABLE,
            location_description=f"{index}번 출구",
            raw_status_text=f"provider-raw-{index}",
        )
        for index in range(7)
    ]
    item = result.items[0].model_copy(
        update={
            "answer_state": FacilityAnswerState.AVAILABLE,
            "facilities": facilities,
        }
    )
    result = result.model_copy(update={"items": [item]})

    presentation = facility_answer_presentation(result)
    public_item = presentation.facility.items[0]
    payload = presentation.model_dump(mode="json")

    assert public_item.answer_state == "AVAILABLE"
    assert public_item.total_count == 7
    assert public_item.truncated is True
    assert len(public_item.facilities) == 5
    serialized = str(payload)
    assert "private-" not in serialized
    assert "provider-raw-" not in serialized


def test_not_found_facility_state_is_projected_as_not_confirmed() -> None:
    result = facility_question_result().facility_result
    assert result is not None
    item = result.items[0].model_copy(
        update={"answer_state": FacilityAnswerState.NOT_FOUND, "facilities": []}
    )
    presentation = facility_answer_presentation(result.model_copy(update={"items": [item]}))

    public_item = presentation.facility.items[0]
    assert public_item.answer_state == "NOT_CONFIRMED"
    assert public_item.total_count == 0
    assert public_item.facilities == ()
