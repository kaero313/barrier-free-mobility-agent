from __future__ import annotations

from typing import Any, TypedDict

from barrier_free_mobility_agent.domain.usage import RunUsage


class AgentState(TypedDict, total=False):
    schema_version: str
    flow_version: str
    current_input: str
    intent_decision: dict[str, Any]
    execution_plan: dict[str, Any]
    trip_context: dict[str, Any]
    mobility_context: dict[str, Any]
    facility_context: dict[str, Any]
    facility_query: str | None
    pending_clarification: dict[str, Any] | None
    origin_resolution: dict[str, Any] | None
    destination_resolution: dict[str, Any] | None
    mcp_profile: dict[str, Any] | None
    projection_assumptions: list[str]
    final_outcome: dict[str, Any] | None
    external_error: dict[str, str] | None
    graph_steps: int
    tool_calls: int
    model_calls: int
    max_parallel_tools_observed: int
    tool_ledger: list[str]
    trajectory: list[str]
    clarification_cycles: int
    run_graph_steps: int
    run_tool_calls: int
    run_tool_ledger: list[str]
    run_trajectory: list[str]
    run_usage: dict[str, Any]


def new_turn_state(
    question: str,
    *,
    schema_version: str,
    flow_version: str,
) -> AgentState:
    return AgentState(
        schema_version=schema_version,
        flow_version=flow_version,
        current_input=question,
        facility_query=None,
        pending_clarification=None,
        origin_resolution=None,
        destination_resolution=None,
        mcp_profile=None,
        projection_assumptions=[],
        final_outcome=None,
        external_error=None,
        graph_steps=0,
        tool_calls=0,
        model_calls=0,
        max_parallel_tools_observed=0,
        tool_ledger=[],
        trajectory=[],
        clarification_cycles=0,
        run_graph_steps=0,
        run_tool_calls=0,
        run_tool_ledger=[],
        run_trajectory=[],
        run_usage=RunUsage().model_dump(mode="json"),
    )
