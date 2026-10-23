from __future__ import annotations

import pytest
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner
from barrier_free_mobility_agent.agent.models import GraphTurnStatus
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.clarification import (
    ChoiceClarificationAnswerValue,
    ClarificationFieldAnswer,
    FieldsClarificationAnswer,
    TextClarificationAnswerValue,
    TriStateClarificationAnswerValue,
)
from barrier_free_mobility_agent.domain.models import (
    ResponseStatus,
    Station,
    StationResolutionResult,
)


def _trip_fields_answer(
    *,
    stairs: str = "no",
    escalator: str = "yes",
) -> FieldsClarificationAnswer:
    return FieldsClarificationAnswer(
        values=(
            ClarificationFieldAnswer(
                input_id="origin",
                value=TextClarificationAnswerValue(value="홍대입구"),
            ),
            ClarificationFieldAnswer(
                input_id="destination",
                value=TextClarificationAnswerValue(value="삼성"),
            ),
            ClarificationFieldAnswer(
                input_id="can_use_stairs",
                value=TriStateClarificationAnswerValue(value=stairs),
            ),
            ClarificationFieldAnswer(
                input_id="can_use_escalator",
                value=TriStateClarificationAnswerValue(value=escalator),
            ),
        )
    )


@pytest.mark.asyncio
async def test_structured_multi_field_resume_applies_slots_without_reparsing_text(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(gateway, settings)
    interrupted = await runner.start(
        "휠체어로 갈 수 있어?",
        thread_id="structured-required-fields",
    )

    completed = await runner.resume(
        _trip_fields_answer(),
        thread_id="structured-required-fields",
    )

    assert interrupted.status == GraphTurnStatus.INTERRUPTED
    assert completed.status == GraphTurnStatus.COMPLETED
    assert completed.state["trip_context"]["origin_query"] == "홍대입구"
    assert completed.state["trip_context"]["destination_query"] == "삼성"
    assert completed.state["mobility_context"]["can_use_stairs"] == {
        "value": "false",
        "source": "user_confirmed",
    }
    assert completed.state["mobility_context"]["can_use_escalator"] == {
        "value": "true",
        "source": "user_confirmed",
    }
    assert gateway.resolve_calls == ["홍대입구", "삼성"]
    assert len(gateway.brief_calls) == 1
    profile = gateway.brief_calls[0][2]
    assert profile.can_use_stairs is False
    assert profile.can_use_escalator is True


@pytest.mark.asyncio
async def test_unknown_required_condition_completes_without_tool_calls(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(gateway, settings)
    await runner.start(
        "휠체어로 갈 수 있어?",
        thread_id="structured-unknown",
    )

    completed = await runner.resume(
        _trip_fields_answer(stairs="unknown", escalator="no"),
        thread_id="structured-unknown",
    )

    assert completed.status == GraphTurnStatus.COMPLETED
    assert completed.outcome.status == ResponseStatus.FAILED
    assert completed.outcome.clarification is None
    assert completed.context.active_clarification is None
    assert completed.outcome.presentation is None
    assert completed.outcome.evidence.unverified_parts == ["계단 이용 가능 여부"]
    assert gateway.resolve_calls == []
    assert gateway.brief_calls == []


@pytest.mark.asyncio
async def test_station_choice_token_selects_candidate_without_label_parsing(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway(
        station_results={
            "시청": StationResolutionResult(
                query="시청",
                matched_station=None,
                candidates=[
                    Station(station_name="시청", line="1호선"),
                    Station(station_name="시청", line="2호선"),
                ],
                needs_clarification=True,
                clarification_message="시청역 노선을 선택해 주세요.",
            )
        }
    )
    runner = MobilityGraphRunner(gateway, settings)
    interrupted = await runner.start(
        (
            "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
            "시청역에서 삼성역까지 갈 수 있어?"
        ),
        thread_id="structured-station-choice",
    )

    completed = await runner.resume(
        FieldsClarificationAnswer(
            values=(
                ClarificationFieldAnswer(
                    input_id="origin",
                    value=ChoiceClarificationAnswerValue(value="option-2"),
                ),
            )
        ),
        thread_id="structured-station-choice",
    )

    assert interrupted.status == GraphTurnStatus.INTERRUPTED
    assert completed.status == GraphTurnStatus.COMPLETED
    assert completed.state["trip_context"]["origin_station"] == "시청"
    assert completed.state["trip_context"]["origin_line"] == "2호선"
    assert gateway.resolve_calls == ["시청", "삼성"]
    assert gateway.brief_calls[0][0] == "2호선 시청"


@pytest.mark.asyncio
async def test_required_fields_message_explains_why_the_conditions_are_needed(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(gateway, settings)

    missing_everything = await runner.start(
        "휠체어로 갈 수 있어?",
        thread_id="required-message-all",
    )
    missing_mobility = await runner.start(
        "휠체어로 홍대입구역에서 삼성역까지 갈 수 있어?",
        thread_id="required-message-mobility",
    )

    # The form title already says more information is needed; the message gives the reason.
    assert missing_everything.outcome.user_message == (
        "출발역과 도착역, 계단·에스컬레이터 이용 여부를 알아야 이동 경로를 확인할 수 있습니다."
    )
    assert missing_mobility.outcome.user_message == (
        "계단·에스컬레이터 이용 여부에 따라 확인해야 할 이동 동선이 달라집니다."
    )
