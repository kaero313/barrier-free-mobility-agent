from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from tests.factories import trip_question_result
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner
from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.application.execution_checkpoint import (
    ExecutionCheckpointState,
)
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.errors import (
    AgentBudgetExceededError,
    AgentStateIncompatibleError,
)
from barrier_free_mobility_agent.domain.identity import LOCAL_OWNER_ID
from barrier_free_mobility_agent.domain.models import (
    ResponseStatus,
    RiskLevel,
    Station,
    StationResolutionResult,
)
from barrier_free_mobility_agent.domain.run_ledger import RunExecutionCorrelation
from barrier_free_mobility_agent.domain.usage import RunUsage, UsageSource

CLEAR_TRIP = (
    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
    "홍대입구역에서 삼성역까지 갈 수 있어?"
)


@pytest.mark.asyncio
async def test_checkpoint_projects_same_attempt_metadata_for_worker_recovery(
    settings: Settings,
) -> None:
    runner = MobilityGraphRunner(FakeMcpGateway(), settings)
    correlation = RunExecutionCorrelation(
        thread_id="checkpoint-metadata",
        run_id="checkpoint-run",
        request_id="checkpoint-request",
        attempt=1,
    )

    turn = await runner.start(
        "서울역에서 시청역까지 갈 수 있어?",
        thread_id=correlation.thread_id,
        correlation=correlation,
    )
    inspection = await runner.inspect_execution_checkpoint(correlation.thread_id)

    assert turn.status == GraphTurnStatus.INTERRUPTED
    assert inspection.state == ExecutionCheckpointState.INTERRUPTED
    assert inspection.cursor is not None
    assert inspection.correlation == correlation


@pytest.mark.asyncio
async def test_clear_trip_executes_fixed_trajectory_and_stores_json_state(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(gateway, settings)

    turn = await runner.start(CLEAR_TRIP, thread_id="clear-trip")

    assert turn.status == GraphTurnStatus.COMPLETED
    assert turn.outcome.user_message == gateway.result.user_message
    assert gateway.resolve_calls == ["홍대입구", "삼성"]
    assert [(origin, destination) for origin, destination, _ in gateway.brief_calls] == [
        ("홍대입구", "삼성")
    ]
    assert turn.state["tool_ledger"] == [
        "resolve_station",
        "resolve_station",
        "generate_accessibility_brief",
    ]
    assert turn.state["model_calls"] == 0
    assert turn.state["max_parallel_tools_observed"] == 1
    assert turn.usage == RunUsage()
    assert turn.state["run_usage"] == RunUsage().model_dump(mode="json")
    assert json.loads(json.dumps(turn.state, ensure_ascii=False)) == turn.state
    assert "result" not in turn.state


@pytest.mark.asyncio
async def test_line_prefixed_stations_and_unspaced_restroom_reach_mcp(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    gateway.station_results = {
        "2호선 시청": StationResolutionResult(
            query="2호선 시청",
            matched_station=Station(station_name="시청", line="2호선"),
        ),
        "9호선 고속터미널": StationResolutionResult(
            query="9호선 고속터미널",
            matched_station=Station(station_name="고속터미널", line="9호선"),
        ),
    }
    runner = MobilityGraphRunner(gateway, settings)

    turn = await runner.start(
        (
            "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
            "장애인화장실도 꼭 필요해요. 2호선 시청역에서 9호선 고속터미널역까지 갈 수 있어?"
        ),
        thread_id="line-prefixed-trip",
    )

    assert turn.status == GraphTurnStatus.COMPLETED
    assert gateway.resolve_calls == ["2호선 시청", "9호선 고속터미널"]
    [(origin, destination, profile)] = gateway.brief_calls
    assert (origin, destination) == ("2호선 시청", "9호선 고속터미널")
    assert profile.need_accessible_restroom is True


@pytest.mark.asyncio
async def test_unknown_abilities_interrupt_before_tools_then_resume_once(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(gateway, settings)

    interrupted = await runner.start(
        "홍대입구역에서 삼성역까지 갈 수 있어?",
        thread_id="clarify-mobility",
    )

    assert interrupted.status == GraphTurnStatus.INTERRUPTED
    assert interrupted.outcome.status == ResponseStatus.NEEDS_CLARIFICATION
    assert gateway.resolve_calls == []
    assert gateway.brief_calls == []

    completed = await runner.resume("둘 다 못 써요", thread_id="clarify-mobility")

    assert completed.status == GraphTurnStatus.COMPLETED
    assert len(gateway.brief_calls) == 1
    profile = gateway.brief_calls[0][2]
    assert profile.can_use_stairs is False
    assert profile.can_use_escalator is False
    mobility = completed.state["mobility_context"]
    assert mobility["wheelchair"]["value"] == "unknown"
    assert mobility["stroller"]["value"] == "unknown"


@pytest.mark.asyncio
async def test_station_candidate_must_be_selected_from_resolver_evidence(
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

    first = await runner.start(
        (
            "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
            "시청역에서 삼성역까지 갈 수 있어?"
        ),
        thread_id="station-choice",
    )
    invalid = await runner.resume("시청", thread_id="station-choice")

    assert first.status == GraphTurnStatus.INTERRUPTED
    assert [option.value for option in first.outcome.clarification_options] == [
        "시청 (1호선)",
        "시청 (2호선)",
    ]
    assert invalid.status == GraphTurnStatus.INTERRUPTED
    assert gateway.resolve_calls == ["시청"]
    assert gateway.brief_calls == []

    completed = await runner.resume("시청 (1호선)", thread_id="station-choice")

    assert completed.status == GraphTurnStatus.COMPLETED
    assert gateway.resolve_calls == ["시청", "삼성"]
    assert len(gateway.brief_calls) == 1


@pytest.mark.asyncio
async def test_invalid_station_resume_attempts_consume_graph_budget(
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
            )
        }
    )
    runner = MobilityGraphRunner(
        gateway,
        settings.model_copy(update={"agent_max_graph_steps": 5}),
    )
    await runner.start(
        (
            "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
            "시청역에서 삼성역까지 갈 수 있어?"
        ),
        thread_id="selection-budget",
    )
    first_invalid = await runner.resume("시청", thread_id="selection-budget")

    assert first_invalid.status == GraphTurnStatus.INTERRUPTED
    with pytest.raises(AgentBudgetExceededError):
        await runner.resume("시청", thread_id="selection-budget")
    assert gateway.resolve_calls == ["시청"]


@pytest.mark.asyncio
async def test_tool_budget_blocks_generate_before_external_call(settings: Settings) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(
        gateway,
        settings.model_copy(update={"agent_max_tool_calls": 2}),
    )

    with pytest.raises(AgentBudgetExceededError):
        await runner.start(CLEAR_TRIP, thread_id="tool-budget")

    assert gateway.resolve_calls == ["홍대입구", "삼성"]
    assert gateway.brief_calls == []


@pytest.mark.asyncio
async def test_graph_step_budget_blocks_generate_before_external_call(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(
        gateway,
        settings.model_copy(update={"agent_max_graph_steps": 4}),
    )

    with pytest.raises(AgentBudgetExceededError):
        await runner.start(CLEAR_TRIP, thread_id="step-budget")

    assert gateway.brief_calls == []


@pytest.mark.asyncio
async def test_partial_unknown_canonical_result_is_preserved(settings: Settings) -> None:
    result = trip_question_result(
        status=ResponseStatus.PARTIAL,
        risk_level=RiskLevel.UNKNOWN,
    )
    assert result.result is not None
    gateway = FakeMcpGateway(result=result, brief_result=result.result)
    runner = MobilityGraphRunner(gateway, settings)

    turn = await runner.start(CLEAR_TRIP, thread_id="partial-result")

    assert turn.outcome.status == ResponseStatus.PARTIAL
    assert turn.outcome.evidence.risk_level == RiskLevel.UNKNOWN
    assert turn.outcome.user_message == result.result.user_message
    assert [source.model_dump(mode="json") for source in turn.outcome.evidence.failed_sources] == [
        {
            "source_name": "facility_information",
            "display_name": "역 시설 정보",
            "recoverable": True,
        }
    ]
    assert "elevator_status" not in str(turn.outcome.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_mcp_clarification_continues_as_a_bounded_new_turn(
    settings: Settings,
) -> None:
    canonical = trip_question_result()
    assert canonical.result is not None
    clarification = canonical.result.model_copy(
        update={
            "status": ResponseStatus.NEEDS_CLARIFICATION,
            "clarification_needed": True,
            "questions": ["엘리베이터만 이용해야 하나요?"],
            "user_message": "이동 조건을 한 가지 더 확인해야 합니다.",
        }
    )
    gateway = FakeMcpGateway(brief_result=clarification)
    runner = MobilityGraphRunner(gateway, settings)

    interrupted = await runner.start(CLEAR_TRIP, thread_id="mcp-clarification")

    assert interrupted.status == GraphTurnStatus.INTERRUPTED
    assert interrupted.resume_mode == ResumeMode.NEW_TURN
    assert interrupted.outcome.status == ResponseStatus.NEEDS_CLARIFICATION

    seeded_usage = RunUsage(
        revision=1,
        model_calls=1,
        input_tokens=20,
        output_tokens=5,
        total_tokens=25,
        source=UsageSource.ESTIMATED,
    )
    await runner._graph.aupdate_state(
        runner._config("mcp-clarification", LOCAL_OWNER_ID),
        {"run_usage": seeded_usage.model_dump(mode="json")},
    )

    gateway.brief_result = canonical.result
    completed = await runner.continue_business_clarification(
        "네",
        thread_id="mcp-clarification",
    )

    assert completed.status == GraphTurnStatus.COMPLETED
    assert completed.resume_mode == ResumeMode.NONE
    assert completed.usage == seeded_usage
    assert completed.state["run_usage"] == seeded_usage.model_dump(mode="json")
    assert completed.state["clarification_cycles"] == 1
    assert len(gateway.brief_calls) == 2
    assert gateway.brief_calls[-1][2].need_elevator_only is True
    assert completed.state["run_tool_calls"] == 6
    assert completed.state["run_tool_ledger"] == [
        "resolve_station",
        "resolve_station",
        "generate_accessibility_brief",
        "resolve_station",
        "resolve_station",
        "generate_accessibility_brief",
    ]


@pytest.mark.asyncio
async def test_unmapped_mcp_clarification_does_not_repeat_tools_without_state_change(
    settings: Settings,
) -> None:
    canonical = trip_question_result()
    assert canonical.result is not None
    gateway = FakeMcpGateway(
        brief_result=canonical.result.model_copy(
            update={
                "status": ResponseStatus.NEEDS_CLARIFICATION,
                "clarification_needed": True,
                "questions": ["휠체어, 유모차, 보행약자 중 어떤 이동 조건인지 알려 주세요."],
                "user_message": "이동 조건을 더 확인해야 합니다.",
            }
        )
    )
    runner = MobilityGraphRunner(gateway, settings)
    first = await runner.start(CLEAR_TRIP, thread_id="unmapped-mcp-question")

    unchanged = await runner.continue_business_clarification(
        "네",
        thread_id="unmapped-mcp-question",
    )

    assert first.resume_mode == ResumeMode.NEW_TURN
    assert unchanged.status == GraphTurnStatus.INTERRUPTED
    assert unchanged.resume_mode == ResumeMode.COMMAND
    assert len(gateway.brief_calls) == 1
    assert unchanged.state["mobility_context"]["stroller"]["value"] == "unknown"

    gateway.brief_result = canonical.result
    completed = await runner.resume(
        "엘리베이터만 이용해야 해요",
        thread_id="unmapped-mcp-question",
    )

    assert completed.status == GraphTurnStatus.COMPLETED
    assert len(gateway.brief_calls) == 2
    assert gateway.brief_calls[-1][2].need_elevator_only is True


@pytest.mark.asyncio
async def test_negative_mcp_question_does_not_invert_yes_answer(
    settings: Settings,
) -> None:
    canonical = trip_question_result()
    assert canonical.result is not None
    gateway = FakeMcpGateway(
        brief_result=canonical.result.model_copy(
            update={
                "status": ResponseStatus.NEEDS_CLARIFICATION,
                "clarification_needed": True,
                "questions": ["계단을 이용할 수 없나요?"],
                "user_message": "계단 이용 조건을 확인해야 합니다.",
            }
        )
    )
    runner = MobilityGraphRunner(gateway, settings)
    await runner.start(CLEAR_TRIP, thread_id="negative-mcp-question")

    unchanged = await runner.continue_business_clarification(
        "네",
        thread_id="negative-mcp-question",
    )

    assert unchanged.status == GraphTurnStatus.INTERRUPTED
    assert unchanged.resume_mode == ResumeMode.COMMAND
    assert unchanged.state["mobility_context"]["can_use_stairs"]["value"] == "false"
    assert len(gateway.brief_calls) == 1


@pytest.mark.asyncio
async def test_mcp_clarification_cycles_bound_cumulative_run_ledger(
    settings: Settings,
) -> None:
    canonical = trip_question_result()
    assert canonical.result is not None
    clarification = canonical.result.model_copy(
        update={
            "status": ResponseStatus.NEEDS_CLARIFICATION,
            "clarification_needed": True,
            "questions": ["엘리베이터만 이용해야 하나요?"],
            "user_message": "이동 조건을 다시 확인해야 합니다.",
        }
    )
    gateway = FakeMcpGateway(brief_result=clarification)
    runner = MobilityGraphRunner(gateway, settings)
    await runner.start(CLEAR_TRIP, thread_id="clarification-budget")
    await runner.continue_business_clarification("네", thread_id="clarification-budget")
    second = await runner.continue_business_clarification(
        "아니요",
        thread_id="clarification-budget",
    )

    with pytest.raises(AgentBudgetExceededError):
        await runner.continue_business_clarification(
            "네",
            thread_id="clarification-budget",
        )

    assert second.status == GraphTurnStatus.INTERRUPTED
    assert second.state["clarification_cycles"] == 2
    assert second.state["run_tool_calls"] == 9
    assert len(second.state["run_tool_ledger"]) == 9
    assert len(gateway.brief_calls) == 3


@pytest.mark.asyncio
async def test_checkpoint_version_mismatch_fails_before_resume(
    settings: Settings,
) -> None:
    checkpointer = InMemorySaver()
    gateway = FakeMcpGateway()
    first_runner = MobilityGraphRunner(gateway, settings, checkpointer=checkpointer)
    await first_runner.start(
        "홍대입구역에서 삼성역까지 갈 수 있어?",
        thread_id="versioned-thread",
    )
    upgraded_runner = MobilityGraphRunner(
        gateway,
        settings.model_copy(update={"agent_flow_version": "trip-v3"}),
        checkpointer=checkpointer,
    )

    with pytest.raises(AgentStateIncompatibleError):
        await upgraded_runner.resume("둘 다 못 써요", thread_id="versioned-thread")

    assert gateway.resolve_calls == []


@pytest.mark.asyncio
async def test_only_lines_the_user_chose_are_sent_with_the_brief(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    gateway.station_results = {
        "2호선 잠실": StationResolutionResult(
            query="2호선 잠실",
            matched_station=Station(station_name="잠실", line="2호선"),
        ),
        "수서": StationResolutionResult(
            query="수서",
            matched_station=Station(station_name="수서", line="3호선"),
        ),
    }
    runner = MobilityGraphRunner(gateway, settings)

    turn = await runner.start(
        (
            "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
            "2호선 잠실역에서 수서역까지 갈 수 있어?"
        ),
        thread_id="unstated-destination-line",
    )

    assert turn.status == GraphTurnStatus.COMPLETED
    # 수서역의 3호선은 역 이름만으로 정해졌다. 사용자가 고른 호선처럼 넘기면 MCP가 수인분당선으로
    # 닿는 경로를 버리므로 역 이름만 넘긴다.
    [(origin, destination, _)] = gateway.brief_calls
    assert (origin, destination) == ("2호선 잠실", "수서")


@pytest.mark.asyncio
async def test_elevator_only_trip_does_not_ask_about_stairs_or_escalators(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(gateway, settings)

    turn = await runner.start(
        "유모차로 홍대입구역에서 삼성역까지 가려는데 엘리베이터만 탈 수 있어",
        thread_id="elevator-only-trip",
    )

    assert turn.status == GraphTurnStatus.COMPLETED
    [(_, _, profile)] = gateway.brief_calls
    assert profile.need_elevator_only is True
    assert profile.can_use_stairs is False
    assert profile.can_use_escalator is False
    # 계단·에스컬레이터는 사용자가 말한 조건이 아니라 엘리베이터만 탄다는 말에서 정한 것이라
    # 확인한 이동 조건으로 따로 보여 주지 않는다.
    shown = {condition.field for condition in turn.context.mobility_conditions}
    assert "can_use_stairs" not in shown
    assert "can_use_escalator" not in shown


@pytest.mark.asyncio
async def test_other_route_follow_up_reuses_the_trip_and_asks_mcp_to_compare(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(gateway, settings)

    first = await runner.start(CLEAR_TRIP, thread_id="alternative-follow-up")
    follow_up = await runner.start("다른 경로 알려줘", thread_id="alternative-follow-up")

    assert first.status == GraphTurnStatus.COMPLETED
    # 범위 밖 질문처럼 다시 묻지 않고, 앞 질문의 역과 이동 조건으로 다른 경로를 비교해 달라고 한다.
    assert follow_up.status == GraphTurnStatus.COMPLETED
    assert [(origin, destination) for origin, destination, _ in gateway.brief_calls] == [
        ("홍대입구", "삼성"),
        ("홍대입구", "삼성"),
    ]
    assert gateway.brief_compare_alternatives == [False, True]
    assert follow_up.context.intent == "trip_accessibility"


@pytest.mark.asyncio
async def test_alternative_route_question_with_both_stations_runs_the_trip_check(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(gateway, settings)

    turn = await runner.start(
        (
            "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
            "홍대입구역에서 삼성역까지 다른 경로 알려줘"
        ),
        thread_id="alternative-first-turn",
    )

    assert turn.status == GraphTurnStatus.COMPLETED
    assert gateway.brief_compare_alternatives == [True]
