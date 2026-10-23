from __future__ import annotations

import pytest
from tests.factories import (
    facility_clarification_result,
    facility_question_result,
)
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner
from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.errors import QuestionTooLongError
from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    ContractStatus,
    McpCapability,
    McpContractSnapshot,
    QuestionIntent,
    ResponseStatus,
)


@pytest.mark.asyncio
async def test_clear_facility_question_uses_one_bounded_composite_call(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway(result=facility_question_result())
    runner = MobilityGraphRunner(gateway, settings)

    turn = await runner.start(
        "강남역 엘리베이터 운행 중이야?",
        thread_id="facility-clear",
    )

    assert turn.status == GraphTurnStatus.COMPLETED
    assert turn.outcome.intent == QuestionIntent.FACILITY_STATUS
    assert turn.outcome.status == ResponseStatus.SUCCESS
    assert turn.outcome.presentation is not None
    assert turn.outcome.presentation.kind == "facility"
    assert gateway.answer_calls == ["강남역 엘리베이터 운행 중이야?"]
    assert gateway.resolve_calls == []
    assert gateway.brief_calls == []
    assert turn.state["tool_ledger"] == ["answer_facility_question"]
    assert turn.state["facility_query"] is None
    assert turn.context.intent == QuestionIntent.FACILITY_STATUS
    assert turn.context.origin is None
    assert turn.context.destination is None
    assert turn.context.mobility_conditions == ()
    assert turn.context.facility is not None
    assert turn.context.facility.target is not None
    assert turn.context.facility.target.station_name == "강남"
    assert [item.answer_state for item in turn.outcome.presentation.facility.items] == ["AVAILABLE"]


@pytest.mark.asyncio
async def test_facility_clarification_resumes_same_run_with_structured_context(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway(result=facility_clarification_result())
    runner = MobilityGraphRunner(gateway, settings)

    interrupted = await runner.start(
        "어느 역 엘리베이터 운행 상태를 알려줘",
        thread_id="facility-clarification",
    )

    assert interrupted.status == GraphTurnStatus.INTERRUPTED
    assert interrupted.resume_mode == ResumeMode.NEW_TURN
    assert interrupted.outcome.intent == QuestionIntent.FACILITY_STATUS
    assert interrupted.outcome.clarification is not None
    assert interrupted.outcome.clarification.prompts[0].fields == ("target_station",)
    assert interrupted.context.facility is not None
    assert interrupted.context.facility.target is None
    assert interrupted.context.facility.requested_types == ("ELEVATOR",)

    gateway.result = facility_question_result()
    completed = await runner.continue_business_clarification(
        "강남역",
        thread_id="facility-clarification",
    )

    assert completed.status == GraphTurnStatus.COMPLETED
    assert gateway.answer_calls == [
        "어느 역 엘리베이터 운행 상태를 알려줘",
        "엘리베이터 운행 상태 강남역",
    ]
    assert completed.state["run_tool_ledger"] == [
        "answer_facility_question",
        "answer_facility_question",
    ]
    assert completed.context.facility is not None
    assert completed.context.facility.target is not None
    assert completed.context.facility.target.station_name == "강남"
    assert completed.context.facility.target_provenance == "user_confirmed"
    assert completed.context.active_clarification is None


@pytest.mark.asyncio
async def test_facility_followup_reuses_target_without_exposing_trip_context(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway(result=facility_question_result())
    runner = MobilityGraphRunner(gateway, settings)
    await runner.start("강남역 엘리베이터 위치 알려줘", thread_id="facility-followup")

    followup = await runner.start(
        "장애인 화장실도 알려줘",
        thread_id="facility-followup",
    )

    assert followup.status == GraphTurnStatus.COMPLETED
    assert gateway.answer_calls[-1].startswith("강남 ")
    assert "장애인 화장실도 알려줘" in gateway.answer_calls[-1]
    assert followup.context.facility is not None
    assert followup.context.facility.target_provenance == "previous_context"
    assert followup.context.origin is None
    assert followup.context.destination is None


@pytest.mark.asyncio
async def test_trip_only_mcp_returns_explicit_facility_capability_failure(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway(
        result=facility_question_result(),
        snapshot=McpContractSnapshot(
            status=ContractStatus.READY,
            capabilities=(McpCapability.TRIP_ACCESSIBILITY,),
        ),
    )
    runner = MobilityGraphRunner(gateway, settings)

    turn = await runner.start("강남역 엘리베이터 알려줘", thread_id="facility-disabled")

    assert turn.status == GraphTurnStatus.COMPLETED
    assert turn.outcome.status == ResponseStatus.FAILED
    assert turn.outcome.source == "agent"
    assert turn.outcome.presentation is None
    assert gateway.answer_calls == []
    assert turn.state["tool_ledger"] == []


@pytest.mark.asyncio
async def test_long_facility_query_is_rejected_before_tool_reservation(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway(result=facility_question_result())
    runner = MobilityGraphRunner(gateway, settings)
    question = "강남역 엘리베이터 " + "상세 위치를 알려주세요 " * 20

    with pytest.raises(QuestionTooLongError):
        await runner.start(question, thread_id="facility-too-long")

    assert gateway.answer_calls == []
    state = await runner.get_state("facility-too-long")
    assert state.get("tool_ledger", []) == []


@pytest.mark.asyncio
async def test_unsupported_facility_type_does_not_call_mcp_facility_workflow(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway(result=facility_question_result())
    runner = MobilityGraphRunner(gateway, settings)

    turn = await runner.start(
        "강남역 에스컬레이터 운행 상태 알려줘",
        thread_id="unsupported-facility",
    )

    assert turn.status == GraphTurnStatus.INTERRUPTED
    assert turn.outcome.intent == QuestionIntent.UNKNOWN
    assert gateway.answer_calls == []


@pytest.mark.asyncio
async def test_numeric_line_is_normalized_for_context_and_followup_query(
    settings: Settings,
) -> None:
    payload = facility_question_result().model_dump(mode="json")
    payload["parsed"]["target_line"] = "2"
    payload["facility_result"]["line"] = "2"
    payload["facility_result"]["items"][0]["facilities"][0]["line"] = "2"
    result = AccessibilityQuestionResult.model_validate(payload)
    gateway = FakeMcpGateway(result=result)
    runner = MobilityGraphRunner(gateway, settings)

    first = await runner.start("2호선 강남역 엘리베이터 위치", thread_id="facility-line")
    followup = await runner.start("장애인 화장실도 알려줘", thread_id="facility-line")

    assert first.context.facility is not None
    assert first.context.facility.target is not None
    assert first.context.facility.target.label == "강남 · 2호선"
    assert gateway.answer_calls[-1].startswith("2호선 강남 ")
    assert followup.status == GraphTurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_line_only_answer_is_attached_to_the_facility_station(
    settings: Settings,
) -> None:
    clarification = facility_clarification_result().model_copy(deep=True)
    clarification.parsed.target_station = "시청"
    clarification.parsed.missing_fields = ["ambiguous_station"]
    clarification.questions = ["시청역은 1호선, 2호선이 있습니다. 어느 호선 기준으로 확인할까요?"]
    gateway = FakeMcpGateway(result=clarification)
    runner = MobilityGraphRunner(gateway, settings)

    interrupted = await runner.start("시청역 엘리베이터 지금 운행해?", thread_id="facility-line")
    assert interrupted.status == GraphTurnStatus.INTERRUPTED

    gateway.result = facility_question_result()
    completed = await runner.continue_business_clarification("2호선", thread_id="facility-line")

    assert completed.status == GraphTurnStatus.COMPLETED
    # 호선을 역 이름 앞에 붙여야 MCP가 그 역의 호선으로 읽는다. 문장 끝에만 두면 같은 질문을
    # 다시 받는다.
    assert gateway.answer_calls[-1].startswith("2호선 시청 ")
