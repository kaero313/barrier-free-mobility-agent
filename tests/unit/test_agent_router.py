from __future__ import annotations

import pytest
from pydantic import ValidationError

from barrier_free_mobility_agent.agent.models import (
    ClarificationKind,
    FacilityContext,
    PendingClarification,
    TripContext,
)
from barrier_free_mobility_agent.agent.policy import ToolPolicy
from barrier_free_mobility_agent.agent.router import DeterministicPreRouter
from barrier_free_mobility_agent.domain.errors import AgentPolicyViolationError
from barrier_free_mobility_agent.domain.mobility import MobilityContext, SlotSource, TriState
from barrier_free_mobility_agent.domain.routing import (
    AgentIntent,
    IntentDecision,
    Playbook,
    ToolCapability,
)


def test_clear_trip_is_routed_deterministically_without_model_fields() -> None:
    router = DeterministicPreRouter()

    first = router.route(
        (
            "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
            "홍대입구역에서 삼성역까지 갈 수 있어?"
        ),
        trip=TripContext(),
        mobility=MobilityContext(),
    )
    second = router.route(
        (
            "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
            "홍대입구역에서 삼성역까지 갈 수 있어?"
        ),
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert first == second
    assert first.decision.intent == AgentIntent.TRIP_ACCESSIBILITY
    assert first.decision.missing_fields == ()
    assert first.trip.origin_query == "홍대입구"
    assert first.trip.destination_query == "삼성"
    assert first.mobility.wheelchair.value == TriState.TRUE
    assert first.mobility.can_use_stairs.source == SlotSource.USER_EXPLICIT


def test_trip_without_abilities_keeps_unknown_and_requests_both_fields() -> None:
    routed = DeterministicPreRouter().route(
        "홍대입구역에서 삼성역까지 갈 수 있어?",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.decision.missing_fields == (
        "can_use_stairs",
        "can_use_escalator",
    )
    assert routed.mobility.can_use_stairs.value == TriState.UNKNOWN
    assert routed.mobility.can_use_escalator.value == TriState.UNKNOWN


def test_wheelchair_does_not_imply_stair_or_escalator_ability() -> None:
    routed = DeterministicPreRouter().route(
        "휠체어로 홍대입구역에서 삼성역까지 갈 수 있어?",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.mobility.wheelchair.value == TriState.TRUE
    assert routed.mobility.can_use_stairs.value == TriState.UNKNOWN
    assert routed.mobility.can_use_escalator.value == TriState.UNKNOWN
    assert routed.mobility.need_elevator_only.value == TriState.UNKNOWN
    assert routed.decision.missing_fields == (
        "can_use_stairs",
        "can_use_escalator",
    )


def test_wheelchair_negation_is_not_misclassified_as_wheelchair_use() -> None:
    routed = DeterministicPreRouter().route(
        "휠체어 없이 계단과 에스컬레이터 이용 가능해요. 홍대입구역에서 삼성역까지 갈 수 있어?",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.mobility.wheelchair.value == TriState.FALSE
    assert routed.mobility.can_use_stairs.value == TriState.TRUE
    assert routed.mobility.can_use_escalator.value == TriState.TRUE
    assert routed.decision.missing_fields == ()


def test_unrelated_followup_does_not_reuse_retained_trip_as_new_intent() -> None:
    routed = DeterministicPreRouter().route(
        "고마워",
        trip=TripContext(
            origin_query="홍대입구",
            destination_query="삼성",
            origin_station="홍대입구",
            destination_station="삼성",
        ),
        mobility=MobilityContext(),
    )

    assert routed.decision.intent == AgentIntent.UNKNOWN


def test_route_pattern_wins_over_facility_word_used_as_a_constraint() -> None:
    routed = DeterministicPreRouter().route(
        "홍대입구역에서 삼성역까지 에스컬레이터 없이 갈 수 있어?",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.decision.intent == AgentIntent.TRIP_ACCESSIBILITY
    assert routed.mobility.can_use_escalator.value == TriState.FALSE


def test_supported_station_facility_query_uses_bounded_facility_capability() -> None:
    routed = DeterministicPreRouter().route(
        "강남역 엘리베이터 운행 중이야?",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.decision.intent == AgentIntent.FACILITY_STATUS
    assert routed.decision.required_capabilities == (ToolCapability.ANSWER_FACILITY_QUESTION,)
    plan = ToolPolicy().build_plan(routed.decision)
    assert plan.playbook == Playbook.STRUCTURED_FACILITY_V1
    assert plan.allowed_tools == (ToolCapability.ANSWER_FACILITY_QUESTION,)


def test_unsupported_facility_term_is_not_sent_to_facility_playbook() -> None:
    routed = DeterministicPreRouter().route(
        "강남역 에스컬레이터 상태 알려줘",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.decision.intent == AgentIntent.UNKNOWN


def test_facility_pending_answer_cannot_fall_into_trip_workflow() -> None:
    pending = PendingClarification(
        kind=ClarificationKind.MCP_CLARIFICATION,
        intent=AgentIntent.FACILITY_STATUS,
        fields=("target_station",),
        questions=("어느 역을 확인할까요?",),
        message="역을 알려주세요.",
    )
    routed = DeterministicPreRouter().route(
        "강남역",
        trip=TripContext(),
        mobility=MobilityContext(),
        facility=FacilityContext(),
        pending=pending,
    )

    assert routed.decision.intent == AgentIntent.FACILITY_STATUS
    assert routed.decision.required_capabilities == (ToolCapability.ANSWER_FACILITY_QUESTION,)


def test_facility_type_followup_uses_only_explicit_facility_context() -> None:
    routed = DeterministicPreRouter().route(
        "장애인 화장실도 알려줘",
        trip=TripContext(),
        mobility=MobilityContext(),
        facility=FacilityContext(target_station="강남"),
    )

    assert routed.decision.intent == AgentIntent.FACILITY_STATUS
    assert "facility_context" in routed.decision.signal_codes


def test_stair_and_escalator_clauses_do_not_share_opposite_values() -> None:
    routed = DeterministicPreRouter().route(
        "홍대입구역에서 삼성역까지 가는데 계단은 가능하지만 에스컬레이터는 못 써요",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.mobility.can_use_stairs.value == TriState.TRUE
    assert routed.mobility.can_use_escalator.value == TriState.FALSE


def test_conjunctive_ability_phrases_keep_each_field_independent() -> None:
    routed = DeterministicPreRouter().route(
        "홍대입구역에서 삼성역까지 가는데 계단은 가능하고 에스컬레이터는 못 써요",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.mobility.can_use_stairs.value == TriState.TRUE
    assert routed.mobility.can_use_escalator.value == TriState.FALSE


def test_resume_confirmation_only_updates_pending_mobility_fields() -> None:
    pending = PendingClarification(
        kind=ClarificationKind.REQUIRED_FIELDS,
        fields=("can_use_stairs", "can_use_escalator"),
        questions=("계단과 에스컬레이터를 이용할 수 있나요?",),
        message="정보가 필요합니다.",
    )
    routed = DeterministicPreRouter().route(
        "둘 다 못 써요",
        trip=TripContext(origin_query="홍대입구", destination_query="삼성"),
        mobility=MobilityContext(),
        pending=pending,
    )

    assert routed.decision.intent == AgentIntent.TRIP_ACCESSIBILITY
    assert routed.decision.missing_fields == ()
    assert routed.mobility.can_use_stairs.value == TriState.FALSE
    assert routed.mobility.can_use_escalator.value == TriState.FALSE
    assert routed.mobility.can_use_stairs.source == SlotSource.USER_CONFIRMED
    assert routed.mobility.wheelchair.value == TriState.UNKNOWN


def test_deictic_origin_uses_only_confirmed_previous_destination() -> None:
    routed = DeterministicPreRouter().route(
        "거기서 시청역까지는?",
        trip=TripContext(
            origin_query="홍대입구",
            destination_query="삼성",
            origin_station="홍대입구",
            destination_station="삼성",
            destination_line="2호선",
        ),
        mobility=MobilityContext(),
    )

    assert routed.trip.origin_query == "삼성"
    assert routed.trip.destination_query == "시청"
    assert routed.trip.origin_line == "2호선"


@pytest.mark.parametrize(
    "text",
    [
        "2호선 시청역에서 9호선 고속터미널역까지 갈 수 있어?",
        "2호선시청에서 9호선고속터미널까지 갈 수 있어?",
        "시청역 2호선에서 고속터미널역 9호선까지 갈 수 있어?",
        "시청(2호선)에서 고속터미널(9호선)까지 갈 수 있어?",
    ],
)
def test_line_written_next_to_station_is_kept_apart_from_station_name(text: str) -> None:
    routed = DeterministicPreRouter().route(
        text,
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.decision.intent == AgentIntent.TRIP_ACCESSIBILITY
    assert "origin" not in routed.decision.missing_fields
    assert "destination" not in routed.decision.missing_fields
    assert routed.trip.origin_query == "시청"
    assert routed.trip.origin_line == "2호선"
    assert routed.trip.destination_query == "고속터미널"
    assert routed.trip.destination_line == "9호선"


def test_line_on_one_endpoint_leaves_other_line_unset() -> None:
    routed = DeterministicPreRouter().route(
        "홍대입구역에서 9호선 고속터미널역까지 휠체어로 갈 수 있어?",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.trip.origin_query == "홍대입구"
    assert routed.trip.origin_line is None
    assert routed.trip.destination_query == "고속터미널"
    assert routed.trip.destination_line == "9호선"


def test_restating_station_with_other_line_drops_previously_resolved_station() -> None:
    previous = TripContext(
        origin_query="홍대입구",
        destination_query="고속터미널",
        origin_station="홍대입구",
        destination_station="고속터미널",
        origin_line="2호선",
        destination_line="3호선",
    )

    same_line = DeterministicPreRouter().route(
        "홍대입구역에서 고속터미널역까지 다시 알려줘",
        trip=previous,
        mobility=MobilityContext(),
    )
    other_line = DeterministicPreRouter().route(
        "홍대입구역에서 9호선 고속터미널역까지 다시 알려줘",
        trip=previous,
        mobility=MobilityContext(),
    )

    assert same_line.trip == previous
    assert other_line.trip.origin_station == "홍대입구"
    assert other_line.trip.origin_line == "2호선"
    assert other_line.trip.destination_station is None
    assert other_line.trip.destination_line == "9호선"


@pytest.mark.parametrize(
    "text",
    [
        "장애인화장실이 꼭 필요하고 휠체어충전도 해야 해요. 홍대입구역에서 삼성역까지",
        "장애인 화장실이 꼭 필요하고 휠체어 충전도 해야 해요. 홍대입구역에서 삼성역까지",
    ],
)
def test_restroom_and_charger_needs_are_detected_with_or_without_space(text: str) -> None:
    routed = DeterministicPreRouter().route(
        text,
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.mobility.need_accessible_restroom.value == TriState.TRUE
    assert routed.mobility.need_wheelchair_charger.value == TriState.TRUE


def test_deictic_origin_without_context_remains_missing() -> None:
    routed = DeterministicPreRouter().route(
        "거기서 시청역까지는?",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.trip.origin_query is None
    assert "origin" in routed.decision.missing_fields


def test_tool_policy_is_the_only_mapping_from_intent_to_tools() -> None:
    decision = (
        DeterministicPreRouter()
        .route(
            (
                "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                "홍대입구역에서 삼성역까지 갈 수 있어?"
            ),
            trip=TripContext(),
            mobility=MobilityContext(),
        )
        .decision
    )
    policy = ToolPolicy()

    plan = policy.build_plan(decision)

    assert plan.playbook == Playbook.STRUCTURED_TRIP_V1
    assert plan.allowed_tools == (
        ToolCapability.RESOLVE_STATION,
        ToolCapability.GENERATE_ACCESSIBILITY_BRIEF,
    )
    with pytest.raises(AgentPolicyViolationError):
        policy.ensure_allowed(
            policy.build_plan(
                IntentDecision(
                    intent=AgentIntent.UNKNOWN,
                    source="deterministic",
                    confidence=0,
                )
            ),
            ToolCapability.RESOLVE_STATION,
            ledger=[],
        )


def test_structured_decision_rejects_injected_tool_name() -> None:
    with pytest.raises(ValidationError):
        IntentDecision.model_validate(
            {
                "intent": "trip_accessibility",
                "source": "model",
                "confidence": 1,
                "tool_name": "unrestricted_tool",
            }
        )


def test_elevator_only_implies_no_stairs_or_escalator_without_asking() -> None:
    routed = DeterministicPreRouter().route(
        "유모차로 서울역에서 강남역까지 가려는데 엘리베이터만 탈 수 있어",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.mobility.need_elevator_only.value == TriState.TRUE
    assert routed.mobility.can_use_stairs.value == TriState.FALSE
    assert routed.mobility.can_use_escalator.value == TriState.FALSE
    assert routed.mobility.can_use_stairs.source == SlotSource.DERIVED_CONSERVATIVE
    assert routed.decision.missing_fields == ()


def test_elevator_only_keeps_an_escalator_answer_the_user_gave() -> None:
    routed = DeterministicPreRouter().route(
        "에스컬레이터는 이용할 수 있어요. 엘리베이터만 있으면 돼요. 시청역에서 강남역까지",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.mobility.can_use_escalator.value == TriState.TRUE
    assert routed.mobility.can_use_escalator.source == SlotSource.USER_EXPLICIT


def test_line_the_user_wrote_is_marked_as_chosen() -> None:
    routed = DeterministicPreRouter().route(
        "2호선 잠실역에서 수서역까지 휠체어로 갈 수 있어?",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.trip.origin_line == "2호선"
    assert routed.trip.origin_line_explicit is True
    assert routed.trip.destination_line is None
    assert routed.trip.destination_line_explicit is False


def test_short_elevator_word_is_a_facility_question() -> None:
    routed = DeterministicPreRouter().route(
        "강남역 엘베 고장났는데 대안 있어?",
        trip=TripContext(),
        mobility=MobilityContext(),
    )

    assert routed.decision.intent == AgentIntent.FACILITY_STATUS
