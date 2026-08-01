from __future__ import annotations

from collections.abc import Mapping

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from tests.factories import trip_question_result
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner, _run_usage_from_state
from barrier_free_mobility_agent.agent.state_budget import (
    canonical_json_bytes,
    ensure_interrupt_payload_within_budget,
    ensure_merged_state_within_budget,
)
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.errors import (
    AgentBudgetExceededError,
    AgentStateIncompatibleError,
    StateInputBudgetExceededError,
)
from barrier_free_mobility_agent.domain.identity import LOCAL_OWNER_ID
from barrier_free_mobility_agent.domain.models import (
    ResponseStatus,
    Station,
    StationResolutionResult,
)
from barrier_free_mobility_agent.domain.usage import RunUsage

_CLEAR_TRIP = (
    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
    "홍대입구역에서 삼성역까지 갈 수 있어?"
)


def test_canonical_json_bytes_are_deterministic_for_korean_and_emoji() -> None:
    value = {"한국": "서울", "emoji": "🚉"}
    expected = '{"emoji":"🚉","한국":"서울"}'.encode()

    assert canonical_json_bytes(value) == expected
    assert canonical_json_bytes({"emoji": "🚉", "한국": "서울"}) == expected


def test_state_and_interrupt_budget_errors_expose_only_numeric_context() -> None:
    with pytest.raises(AgentBudgetExceededError) as state_error:
        ensure_merged_state_within_budget(
            {"retained": "서울"},
            {"current_input": "🚉" * 8},
            limit=16,
        )
    with pytest.raises(AgentBudgetExceededError) as interrupt_error:
        ensure_interrupt_payload_within_budget(
            {"question": "서울역을 선택해 주세요."},
            limit=16,
        )

    assert state_error.value.context == {
        "budget": "state_bytes",
        "limit": 16,
        "actual": len(canonical_json_bytes({"retained": "서울", "current_input": "🚉" * 8})),
    }
    assert interrupt_error.value.context == {
        "budget": "interrupt_payload_bytes",
        "limit": 16,
        "actual": len(canonical_json_bytes({"question": "서울역을 선택해 주세요."})),
    }


@pytest.mark.asyncio
async def test_oversized_state_input_is_rejected_before_any_checkpoint() -> None:
    checkpointer = InMemorySaver()
    runner = MobilityGraphRunner(
        FakeMcpGateway(),
        Settings(_env_file=None, agent_state_max_bytes=1024),
        checkpointer=checkpointer,
    )

    with pytest.raises(AgentBudgetExceededError) as exc_info:
        await runner.start("🚉" * 300, thread_id="oversized-input")

    assert exc_info.value.context["budget"] == "state_bytes"
    assert await runner.get_state("oversized-input") == {}
    checkpoints = [
        checkpoint
        async for checkpoint in checkpointer.alist(
            {"configurable": {"thread_id": "local-test-owner:oversized-input"}}
        )
    ]
    assert checkpoints == []


@pytest.mark.asyncio
async def test_oversized_node_update_is_not_checkpointed() -> None:
    runner = MobilityGraphRunner(
        FakeMcpGateway(),
        Settings(_env_file=None, agent_state_max_bytes=1024),
    )
    question = "홍대입구역에서 삼성역까지 갈 수 있어?"

    with pytest.raises(AgentBudgetExceededError) as exc_info:
        await runner.start(question, thread_id="oversized-node")

    state = await runner.get_state("oversized-node")
    assert exc_info.value.context["budget"] == "state_bytes"
    assert state["current_input"] == question
    assert "intent_decision" not in state
    assert state["trajectory"] == []


@pytest.mark.asyncio
async def test_oversized_business_clarification_input_adds_no_checkpoint() -> None:
    canonical = trip_question_result()
    assert canonical.result is not None
    gateway = FakeMcpGateway(
        brief_result=canonical.result.model_copy(
            update={
                "status": ResponseStatus.NEEDS_CLARIFICATION,
                "clarification_needed": True,
                "questions": ["엘리베이터만 이용해야 하나요?"],
                "user_message": "이동 조건을 한 가지 더 확인해야 합니다.",
            }
        )
    )
    checkpointer = InMemorySaver()
    initial_runner = MobilityGraphRunner(
        gateway,
        Settings(_env_file=None),
        checkpointer=checkpointer,
    )
    await initial_runner.start(_CLEAR_TRIP, thread_id="business-input-budget")
    retained = await initial_runner.get_state("business-input-budget")
    budget = max(1024, len(canonical_json_bytes(retained)) + 100)
    config: RunnableConfig = {
        "configurable": {"thread_id": "local-test-owner:business-input-budget"}
    }
    before = [checkpoint async for checkpoint in checkpointer.alist(config)]
    constrained_runner = MobilityGraphRunner(
        gateway,
        Settings(_env_file=None, agent_state_max_bytes=budget),
        checkpointer=checkpointer,
    )

    with pytest.raises(StateInputBudgetExceededError) as exc_info:
        await constrained_runner.continue_business_clarification(
            "🚉" * 2000,
            thread_id="business-input-budget",
        )

    after = [checkpoint async for checkpoint in checkpointer.alist(config)]
    assert exc_info.value.context["budget"] == "state_bytes"
    assert [item.config for item in after] == [item.config for item in before]


def test_legacy_checkpoint_without_run_usage_reads_as_zero() -> None:
    assert _run_usage_from_state({}) == RunUsage()


@pytest.mark.parametrize("operation", ["start", "resume", "business"])
@pytest.mark.parametrize(
    "invalid_usage",
    [
        {"schema_version": "999"},
        {"schema_version": "1", "revision": "0"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_checkpoint_usage_fails_before_graph_or_tool_mutation(
    operation: str,
    invalid_usage: dict[str, object],
) -> None:
    checkpointer = InMemorySaver()
    gateway = FakeMcpGateway()
    runner = MobilityGraphRunner(
        gateway,
        Settings(_env_file=None),
        checkpointer=checkpointer,
    )
    thread_id = f"invalid-usage-{operation}-{invalid_usage['schema_version']}"
    await runner.start(
        "홍대입구역에서 삼성역까지 갈 수 있어?",
        thread_id=thread_id,
    )
    config = runner._config(thread_id, LOCAL_OWNER_ID)
    await runner._graph.aupdate_state(config, {"run_usage": invalid_usage})
    before = [checkpoint async for checkpoint in checkpointer.alist(config)]

    if operation == "start":
        invocation = runner.start("다시 질문할게요", thread_id=thread_id)
    elif operation == "resume":
        invocation = runner.resume("둘 다 못 써요", thread_id=thread_id)
    else:
        invocation = runner.continue_business_clarification(
            "둘 다 못 써요",
            thread_id=thread_id,
        )
    with pytest.raises(AgentStateIncompatibleError) as exc_info:
        await invocation

    after = [checkpoint async for checkpoint in checkpointer.alist(config)]
    assert exc_info.value.context == {"expected_usage_schema_version": "1"}
    assert [item.config for item in after] == [item.config for item in before]
    assert gateway.resolve_calls == []
    assert gateway.brief_calls == []


@pytest.mark.asyncio
async def test_every_interrupt_site_checks_payload_before_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_kinds: list[str] = []
    original = ensure_interrupt_payload_within_budget

    def recording_guard(payload: object, *, limit: int) -> int:
        if isinstance(payload, Mapping) and isinstance((kind := payload.get("kind")), str):
            seen_kinds.append(kind)
        return original(payload, limit=limit)

    monkeypatch.setattr(
        "barrier_free_mobility_agent.agent.graph.ensure_interrupt_payload_within_budget",
        recording_guard,
    )
    await MobilityGraphRunner(FakeMcpGateway(), Settings(_env_file=None)).start(
        "홍대입구역에서 삼성역까지 갈 수 있어?",
        thread_id="required-fields-interrupt",
    )

    station = "시청"
    station_gateway = FakeMcpGateway(
        station_results={
            station: StationResolutionResult(
                query=station,
                matched_station=None,
                candidates=[
                    Station(station_name=station, line="1호선"),
                    Station(station_name=station, line="2호선"),
                ],
                needs_clarification=True,
            )
        }
    )
    await MobilityGraphRunner(station_gateway, Settings(_env_file=None)).start(
        "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
        "시청역에서 삼성역까지 갈 수 있어?",
        thread_id="station-selection-interrupt",
    )

    assert seen_kinds == ["required_fields", "station_selection"]


@pytest.mark.asyncio
async def test_interrupt_guard_failure_happens_before_framework_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def rejecting_guard(payload: object, *, limit: int) -> int:
        del payload, limit
        raise AgentBudgetExceededError(
            context={
                "budget": "interrupt_payload_bytes",
                "limit": 65_536,
                "actual": 65_537,
            }
        )

    monkeypatch.setattr(
        "barrier_free_mobility_agent.agent.graph.ensure_interrupt_payload_within_budget",
        rejecting_guard,
    )
    runner = MobilityGraphRunner(FakeMcpGateway(), Settings(_env_file=None))

    with pytest.raises(AgentBudgetExceededError):
        await runner.start(
            "홍대입구역에서 삼성역까지 갈 수 있어?",
            thread_id="interrupt-rejected",
        )

    snapshot = await runner._graph.aget_state(
        runner._config("interrupt-rejected", LOCAL_OWNER_ID),
        subgraphs=True,
    )
    assert all(not task.interrupts for task in snapshot.tasks)
