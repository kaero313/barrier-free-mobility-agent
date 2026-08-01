from __future__ import annotations

from typing import Any

from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    ResponseStatus,
    RiskLevel,
)

QUESTION = (
    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
    "홍대입구역에서 삼성역까지 갈 수 있어?"
)
CANONICAL_MESSAGE = "조회 결과 일부 구간에서 주의가 필요합니다.\n출발 직전 다시 확인하세요."


def mobility_profile() -> dict[str, Any]:
    return {
        "wheelchair": True,
        "stroller": False,
        "cane_or_walker": False,
        "can_use_stairs": False,
        "can_use_escalator": False,
        "need_elevator_only": True,
        "need_accessible_restroom": False,
        "accessible_restroom_requirement": "all_key_stations",
        "need_wheelchair_charger": False,
        "avoid_many_transfers": True,
        "max_transfer_count": 1,
    }


def parsed_question() -> dict[str, Any]:
    return {
        "origin": "홍대입구",
        "destination": "삼성",
        "target_station": None,
        "target_line": None,
        "facility_types": [],
        "facility_question_kind": None,
        "alternative_request_kind": None,
        "mobility_profile": mobility_profile(),
        "station_mentions": ["홍대입구", "삼성"],
        "place_mentions": [],
        "missing_fields": [],
    }


def trip_question_result(
    *,
    status: ResponseStatus = ResponseStatus.SUCCESS,
    risk_level: RiskLevel = RiskLevel.CAUTION,
    message: str = CANONICAL_MESSAGE,
) -> AccessibilityQuestionResult:
    failed_sources: list[dict[str, Any]] = []
    limitations: list[str] = []
    if status == ResponseStatus.PARTIAL:
        failed_sources = [
            {
                "source_name": "elevator_status",
                "reason": "temporary failure",
                "recoverable": True,
            }
        ]
        limitations = ["일부 승강기 상태를 확인하지 못했습니다."]

    payload: dict[str, Any] = {
        "question": QUESTION,
        "status": status,
        "intent": "trip_accessibility",
        "parsed": parsed_question(),
        "result": {
            "status": status,
            "origin": "홍대입구",
            "destination": "삼성",
            "mobility_profile": mobility_profile(),
            "risk_level": risk_level,
            "risk_score": 35,
            "route_summary": "홍대입구에서 삼성까지 1회 환승",
            "failed_sources": failed_sources,
            "limitations": limitations,
            "confidence_level": "MEDIUM",
            "confidence_reasons": ["mock fixture"],
            "last_checked_at": "2026-07-10T03:00:00Z",
            "evidence_sources": [],
            "unverified_parts": limitations,
            "clarification_needed": False,
            "questions": [],
            "available_partial_info": [],
            "safety_notice": "출발 직전 재확인하세요.",
            "user_message": message,
            "future_additive_field": "ignored",
        },
        "facility_result": None,
        "user_message": message,
        "clarification_needed": False,
        "questions": [],
        "available_partial_info": [],
        "future_top_level_field": "ignored",
    }
    return AccessibilityQuestionResult.model_validate(payload)


def facility_question_result() -> AccessibilityQuestionResult:
    message = "강남역 엘리베이터는 현재 이용 가능합니다."
    payload: dict[str, Any] = {
        "question": "강남역 엘리베이터 운행 중이야?",
        "status": "SUCCESS",
        "intent": "facility_status",
        "parsed": {
            **parsed_question(),
            "origin": None,
            "destination": None,
            "target_station": "강남",
            "facility_types": ["ELEVATOR"],
            "station_mentions": ["강남"],
        },
        "result": None,
        "facility_result": {
            "status": "SUCCESS",
            "station_name": "강남",
            "line": None,
            "question_kind": "status",
            "items": [
                {
                    "facility_type": "ELEVATOR",
                    "answer_state": "AVAILABLE",
                    "facilities": [
                        {
                            "station_name": "강남",
                            "line": "2호선",
                            "facility_type": "ELEVATOR",
                            "status": "AVAILABLE",
                            "location_description": "1번 출구",
                        }
                    ],
                }
            ],
            "last_checked_at": "2026-07-10T03:00:00Z",
            "evidence_sources": [],
            "failed_sources": [],
            "limitations": [],
            "unverified_parts": [],
            "safety_notice": "출발 직전 재확인하세요.",
            "user_message": message,
        },
        "user_message": message,
        "clarification_needed": False,
        "questions": [],
        "available_partial_info": [],
    }
    return AccessibilityQuestionResult.model_validate(payload)


def facility_clarification_result() -> AccessibilityQuestionResult:
    message = "조회할 역을 알려주세요."
    payload: dict[str, Any] = {
        "question": "어느 역 엘리베이터 운행 상태를 알려줘",
        "status": "NEEDS_CLARIFICATION",
        "intent": "facility_status",
        "parsed": {
            **parsed_question(),
            "origin": None,
            "destination": None,
            "target_station": None,
            "facility_types": ["ELEVATOR"],
            "facility_question_kind": "status",
            "station_mentions": [],
            "missing_fields": ["target_station"],
        },
        "result": None,
        "facility_result": None,
        "user_message": message,
        "clarification_needed": True,
        "questions": ["어느 역의 엘리베이터를 확인할까요?"],
        "available_partial_info": ["엘리베이터 운행 상태 조회"],
    }
    return AccessibilityQuestionResult.model_validate(payload)


def clarification_result() -> AccessibilityQuestionResult:
    message = "출발역과 도착역을 알려주세요."
    payload: dict[str, Any] = {
        "question": "휠체어로 갈 수 있어?",
        "status": "NEEDS_CLARIFICATION",
        "intent": "trip_accessibility",
        "parsed": {
            **parsed_question(),
            "origin": None,
            "destination": None,
            "missing_fields": ["origin", "destination"],
        },
        "result": None,
        "facility_result": None,
        "user_message": message,
        "clarification_needed": True,
        "questions": ["출발역은 어디인가요?", "도착역은 어디인가요?"],
        "available_partial_info": [],
    }
    return AccessibilityQuestionResult.model_validate(payload)
