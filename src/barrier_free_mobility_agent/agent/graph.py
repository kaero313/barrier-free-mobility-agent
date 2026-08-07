from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
from time import monotonic_ns
from typing import Literal, cast
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, GraphOutput, interrupt
from pydantic import ValidationError

from barrier_free_mobility_agent.agent.models import (
    ClarificationKind,
    ExecutionLimits,
    FacilityContext,
    GraphTurn,
    GraphTurnStatus,
    PendingClarification,
    PendingPrompt,
    ResumeMode,
    TripContext,
)
from barrier_free_mobility_agent.agent.policy import ToolPolicy
from barrier_free_mobility_agent.agent.projections import (
    project_conversation_context,
    project_pending_clarification,
)
from barrier_free_mobility_agent.agent.router import DeterministicPreRouter, normalize_input
from barrier_free_mobility_agent.agent.state import AgentState, new_turn_state
from barrier_free_mobility_agent.agent.state_budget import (
    ensure_interrupt_payload_within_budget,
    ensure_merged_state_within_budget,
)
from barrier_free_mobility_agent.application.checkpoint_fencing import (
    attach_execution_lease,
)
from barrier_free_mobility_agent.application.execution_checkpoint import (
    ExecutionCheckpointInspection,
    ExecutionCheckpointState,
)
from barrier_free_mobility_agent.application.ports import MobilityMcpGateway
from barrier_free_mobility_agent.application.run_ledger import RunLedgerWriter
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.clarification import (
    ChoiceClarificationAnswerValue,
    FieldsClarificationAnswer,
    TextClarificationAnswerValue,
    TriStateClarificationAnswerValue,
)
from barrier_free_mobility_agent.domain.errors import (
    AgentBudgetExceededError,
    AgentError,
    AgentPolicyViolationError,
    AgentStateIncompatibleError,
    InvalidQuestionError,
    McpContractError,
    McpDependencyDegradedError,
    McpResponseValidationError,
    McpTimeoutError,
    McpToolExecutionError,
    McpUnavailableError,
    QuestionTooLongError,
    StateInputBudgetExceededError,
)
from barrier_free_mobility_agent.domain.execution_queue import ExecutionLease
from barrier_free_mobility_agent.domain.identity import LOCAL_OWNER_ID, OwnerId
from barrier_free_mobility_agent.domain.mobility import (
    MobilityContext,
    MobilityFlag,
    SlotSource,
    project_mobility_context,
)
from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    AccessibilityResult,
    AnswerEvidence,
    AnswerOutcome,
    AnswerSource,
    FacilityQuestionKind,
    FacilityType,
    McpCapability,
    MobilityProfile,
    QuestionIntent,
    ResponseStatus,
    Station,
    StationResolutionResult,
    project_failed_source_summaries,
    trip_answer_presentation,
)
from barrier_free_mobility_agent.domain.routing import (
    AgentIntent,
    DecisionSource,
    ExecutionPlan,
    IntentDecision,
    Playbook,
    ToolCapability,
)
from barrier_free_mobility_agent.domain.run_ledger import (
    AnswerFacilityQuestionToolSummary,
    GenerateAccessibilityBriefToolSummary,
    ResolveStationToolSummary,
    RunExecutionCorrelation,
    ToolExecutionSummary,
    ToolFailureCode,
    ToolTransition,
)
from barrier_free_mobility_agent.domain.usage import RUN_USAGE_SCHEMA_VERSION, RunUsage


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    gateway: MobilityMcpGateway
    limits: ExecutionLimits
    flow_version: str
    state_schema_version: str
    owner_id: OwnerId
    correlation: RunExecutionCorrelation | None
    run_ledger: RunLedgerWriter | None
    execution_lease: ExecutionLease | None


type MobilityGraph = CompiledStateGraph[
    AgentState,
    RuntimeContext,
    AgentState,
    AgentState,
]
type ApplicationNode = Callable[..., Awaitable[AgentState]]

_MCP_ERROR_BY_CODE: dict[str, type[AgentError]] = {
    error_type.code: error_type
    for error_type in (
        McpUnavailableError,
        McpContractError,
        McpDependencyDegradedError,
        McpToolExecutionError,
        McpTimeoutError,
        McpResponseValidationError,
    )
}


class MobilityGraphRunner:
    """Runs capability-gated mobility workflows with owner-scoped checkpoints."""

    def __init__(
        self,
        gateway: MobilityMcpGateway,
        settings: Settings,
        *,
        checkpointer: BaseCheckpointSaver[str] | None = None,
        run_ledger: RunLedgerWriter | None = None,
    ) -> None:
        self._settings = settings
        self._checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self._gateway = gateway
        self._run_ledger = run_ledger
        self._execution_limits = ExecutionLimits(
            max_graph_steps=settings.agent_max_graph_steps,
            max_tool_calls=settings.agent_max_tool_calls,
            max_model_calls=settings.agent_max_model_calls,
            max_parallel_tools=settings.agent_max_parallel_tools,
            max_clarification_cycles=settings.agent_max_clarification_cycles,
        )
        self._router = DeterministicPreRouter()
        self._policy = ToolPolicy()
        self._graph = self._build_graph()

    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
        execution_lease: ExecutionLease | None = None,
    ) -> GraphTurn:
        normalized = normalize_input(question)
        if not normalized:
            raise InvalidQuestionError()
        if len(normalized) > self._settings.api_request_max_chars:
            raise QuestionTooLongError(
                context={
                    "max_chars": self._settings.api_request_max_chars,
                    "actual_chars": len(normalized),
                }
            )
        active_thread_id = thread_id or uuid4().hex
        await self._ensure_checkpoint_compatible(active_thread_id, owner_id)
        state_input = new_turn_state(
            normalized,
            schema_version=self._settings.agent_state_schema_version,
            flow_version=self._settings.agent_flow_version,
        )
        await self._ensure_state_input_within_budget(
            active_thread_id,
            owner_id,
            state_input,
        )
        result = await self._graph.ainvoke(
            state_input,
            config=self._config(
                active_thread_id,
                owner_id,
                correlation=correlation,
                execution_lease=execution_lease,
            ),
            context=self._runtime_context_for(
                active_thread_id,
                owner_id=owner_id,
                correlation=correlation,
                execution_lease=execution_lease,
            ),
            version="v2",
        )
        return self._to_turn(result)

    def supports(self, question: str) -> bool:
        routed = self._router.route(
            question,
            trip=TripContext(),
            mobility=MobilityContext(),
            facility=FacilityContext(),
        )
        return routed.decision.intent == AgentIntent.TRIP_ACCESSIBILITY or (
            routed.decision.intent == AgentIntent.FACILITY_STATUS
            and McpCapability.FACILITY_QUESTION in self._gateway.contract_snapshot.capabilities
        )

    async def execute(self, question: str) -> AnswerOutcome:
        thread_id = uuid4().hex
        try:
            return (await self.start(question, thread_id=thread_id)).outcome
        finally:
            await self.delete_thread(thread_id)

    async def resume(
        self,
        answer: str | FieldsClarificationAnswer,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
        execution_lease: ExecutionLease | None = None,
    ) -> GraphTurn:
        if isinstance(answer, str):
            resume_payload: str | dict[str, object] = normalize_input(answer)
            if not resume_payload:
                raise InvalidQuestionError()
            if len(resume_payload) > self._settings.api_request_max_chars:
                raise QuestionTooLongError(
                    context={
                        "max_chars": self._settings.api_request_max_chars,
                        "actual_chars": len(resume_payload),
                    }
                )
        else:
            resume_payload = answer.model_dump(mode="json")
        await self._ensure_checkpoint_compatible(thread_id, owner_id)
        result = await self._graph.ainvoke(
            Command(resume=resume_payload),
            config=self._config(
                thread_id,
                owner_id,
                correlation=correlation,
                execution_lease=execution_lease,
            ),
            context=self._runtime_context_for(
                thread_id,
                owner_id=owner_id,
                correlation=correlation,
                execution_lease=execution_lease,
            ),
            version="v2",
        )
        return self._to_turn(result)

    async def continue_business_clarification(
        self,
        answer: str,
        *,
        thread_id: str,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        correlation: RunExecutionCorrelation | None = None,
        execution_lease: ExecutionLease | None = None,
    ) -> GraphTurn:
        """Start a bounded follow-up turn for a clarification returned by MCP."""

        normalized = normalize_input(answer)
        if not normalized:
            raise InvalidQuestionError()
        if len(normalized) > self._settings.api_request_max_chars:
            raise QuestionTooLongError(
                context={
                    "max_chars": self._settings.api_request_max_chars,
                    "actual_chars": len(normalized),
                }
            )
        await self._ensure_checkpoint_compatible(thread_id, owner_id)
        snapshot = await self._graph.aget_state(self._config(thread_id, owner_id))
        state = cast(AgentState, snapshot.values)
        if state.get("pending_clarification") is None:
            raise AgentPolicyViolationError(context={"reason": "clarification_state_missing"})
        clarification_cycles = state.get("clarification_cycles", 0) + 1
        if clarification_cycles > self._settings.agent_max_clarification_cycles:
            raise AgentBudgetExceededError(
                context={
                    "budget": "clarification_cycles",
                    "limit": self._settings.agent_max_clarification_cycles,
                }
            )
        state_input = AgentState(
            current_input=normalized,
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
            clarification_cycles=clarification_cycles,
        )
        self._ensure_input_merge_within_budget(state, state_input)
        result = await self._graph.ainvoke(
            state_input,
            config=self._config(
                thread_id,
                owner_id,
                correlation=correlation,
                execution_lease=execution_lease,
            ),
            context=self._runtime_context_for(
                thread_id,
                owner_id=owner_id,
                correlation=correlation,
                execution_lease=execution_lease,
            ),
            version="v2",
        )
        return self._to_turn(result)

    async def get_state(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId = LOCAL_OWNER_ID,
    ) -> AgentState:
        snapshot = await self._graph.aget_state(self._config(thread_id, owner_id))
        return cast(AgentState, dict(snapshot.values))

    async def inspect_execution_checkpoint(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId = LOCAL_OWNER_ID,
        checkpoint_cursor: str | None = None,
    ) -> ExecutionCheckpointInspection:
        """Project only metadata needed for fail-closed worker recovery."""

        snapshot = await self._graph.aget_state(
            self._config(
                thread_id,
                owner_id,
                checkpoint_cursor=checkpoint_cursor,
            )
        )
        if not snapshot.values:
            return ExecutionCheckpointInspection(state=ExecutionCheckpointState.MISSING)
        state = cast(AgentState, snapshot.values)
        self._validate_checkpoint_state(state)
        cursor = _checkpoint_cursor(snapshot.config)
        if cursor is None:
            raise AgentStateIncompatibleError(context={"reason": "checkpoint_cursor_missing"})
        correlation = _checkpoint_correlation(
            snapshot.metadata,
            thread_id=thread_id,
        )
        execution_job_id, lease_generation = _checkpoint_execution_provenance(snapshot.metadata)
        if snapshot.interrupts:
            phase = ExecutionCheckpointState.INTERRUPTED
        elif snapshot.next:
            phase = ExecutionCheckpointState.CONTINUABLE
        else:
            phase = ExecutionCheckpointState.TERMINAL
        return ExecutionCheckpointInspection(
            state=phase,
            cursor=cursor,
            correlation=correlation,
            execution_job_id=execution_job_id,
            lease_generation=lease_generation,
        )

    async def recover_execution_checkpoint(
        self,
        *,
        thread_id: str,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation,
        execution_lease: ExecutionLease,
        checkpoint_cursor: str,
    ) -> GraphTurn:
        """Continue only the exact checkpoint written by the same execution attempt."""

        config = self._config(
            thread_id,
            owner_id,
            correlation=correlation,
            checkpoint_cursor=checkpoint_cursor,
            execution_lease=execution_lease,
        )
        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            raise AgentStateIncompatibleError(context={"reason": "checkpoint_missing"})
        state = cast(AgentState, snapshot.values)
        self._validate_checkpoint_state(state)
        stored_correlation = _checkpoint_correlation(
            snapshot.metadata,
            thread_id=thread_id,
        )
        if stored_correlation != correlation:
            raise AgentStateIncompatibleError(
                context={"reason": "checkpoint_execution_correlation_mismatch"}
            )
        stored_job_id, stored_generation = _checkpoint_execution_provenance(snapshot.metadata)
        if (
            stored_job_id != execution_lease.job_id
            or stored_generation is None
            or stored_generation > execution_lease.generation
        ):
            raise AgentStateIncompatibleError(
                context={"reason": "checkpoint_execution_lease_provenance_mismatch"}
            )
        if snapshot.interrupts or not snapshot.next:
            return self._to_turn(
                GraphOutput(
                    value=state,
                    interrupts=snapshot.interrupts,
                )
            )
        result = await self._graph.ainvoke(
            None,
            config=config,
            context=self._runtime_context_for(
                thread_id,
                owner_id=owner_id,
                correlation=correlation,
                execution_lease=execution_lease,
            ),
            version="v2",
        )
        return self._to_turn(result)

    async def delete_thread(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId = LOCAL_OWNER_ID,
    ) -> None:
        await self._checkpointer.adelete_thread(self._checkpoint_thread_id(thread_id, owner_id))

    async def _ensure_checkpoint_compatible(
        self,
        thread_id: str,
        owner_id: OwnerId,
    ) -> None:
        snapshot = await self._graph.aget_state(self._config(thread_id, owner_id))
        if not snapshot.values:
            return
        state = cast(AgentState, snapshot.values)
        self._validate_checkpoint_state(state)

    def _validate_checkpoint_state(self, state: AgentState) -> None:
        if (
            state.get("schema_version") != self._settings.agent_state_schema_version
            or state.get("flow_version") != self._settings.agent_flow_version
        ):
            raise AgentStateIncompatibleError(
                context={
                    "expected_schema_version": self._settings.agent_state_schema_version,
                    "expected_flow_version": self._settings.agent_flow_version,
                }
            )
        _run_usage_from_state(state)

    async def _ensure_state_input_within_budget(
        self,
        thread_id: str,
        owner_id: OwnerId,
        state_input: AgentState,
    ) -> None:
        snapshot = await self._graph.aget_state(self._config(thread_id, owner_id))
        self._ensure_input_merge_within_budget(
            cast(AgentState, snapshot.values),
            state_input,
        )

    def _ensure_input_merge_within_budget(
        self,
        state: Mapping[str, object],
        state_input: Mapping[str, object],
    ) -> None:
        try:
            ensure_merged_state_within_budget(
                state,
                state_input,
                limit=self._settings.agent_state_max_bytes,
            )
        except AgentBudgetExceededError as exc:
            raise StateInputBudgetExceededError(context=exc.context) from exc

    def _bounded_node(self, node: ApplicationNode) -> ApplicationNode:
        @wraps(node)
        async def bounded(
            state: AgentState,
            *,
            runtime: Runtime[RuntimeContext],
        ) -> AgentState:
            update = await node(state, runtime=runtime)
            ensure_merged_state_within_budget(
                state,
                update,
                limit=self._settings.agent_state_max_bytes,
            )
            return update

        return bounded

    def _interrupt(self, payload: object) -> object:
        ensure_interrupt_payload_within_budget(
            payload,
            limit=self._settings.agent_state_max_bytes,
        )
        return interrupt(payload)

    def _build_graph(self) -> MobilityGraph:
        trip_graph = self._build_trip_graph()
        facility_graph = self._build_facility_graph()
        builder = StateGraph(AgentState, context_schema=RuntimeContext)
        builder.add_node("route", self._bounded_node(self._route_node))
        builder.add_node("clarify", self._bounded_node(self._clarify_node))
        builder.add_node("trip_workflow", trip_graph)
        builder.add_node("facility_workflow", facility_graph)
        builder.add_node(
            "raise_external_error",
            self._bounded_node(self._raise_external_error_node),
        )
        builder.add_edge(START, "route")
        builder.add_conditional_edges(
            "route",
            self._route_target,
            {
                "clarify": "clarify",
                "trip_workflow": "trip_workflow",
                "facility_workflow": "facility_workflow",
            },
        )
        builder.add_conditional_edges(
            "clarify",
            self._clarification_target,
            {"route": "route", "end": END},
        )
        builder.add_conditional_edges(
            "trip_workflow",
            self._trip_workflow_target,
            {"raise_external_error": "raise_external_error", "end": END},
        )
        builder.add_conditional_edges(
            "facility_workflow",
            self._facility_workflow_target,
            {"raise_external_error": "raise_external_error", "end": END},
        )
        builder.add_edge("raise_external_error", END)
        return builder.compile(checkpointer=self._checkpointer)

    def _build_facility_graph(self) -> MobilityGraph:
        builder = StateGraph(AgentState, context_schema=RuntimeContext)
        builder.add_node("prepare_facility", self._bounded_node(self._prepare_facility_node))
        builder.add_node("reserve_facility", self._bounded_node(self._reserve_facility_node))
        builder.add_node("answer_facility", self._bounded_node(self._answer_facility_node))
        builder.add_edge(START, "prepare_facility")
        builder.add_conditional_edges(
            "prepare_facility",
            self._prepared_facility_target,
            {"reserve_facility": "reserve_facility", "end": END},
        )
        builder.add_edge("reserve_facility", "answer_facility")
        builder.add_edge("answer_facility", END)
        return builder.compile()

    def _build_trip_graph(self) -> MobilityGraph:
        builder = StateGraph(AgentState, context_schema=RuntimeContext)
        builder.add_node("project_mobility", self._bounded_node(self._project_mobility_node))
        builder.add_node("reserve_origin", self._bounded_node(self._reserve_origin_node))
        builder.add_node("resolve_origin", self._bounded_node(self._resolve_origin_node))
        builder.add_node("select_origin", self._bounded_node(self._select_origin_node))
        builder.add_node(
            "reserve_destination",
            self._bounded_node(self._reserve_destination_node),
        )
        builder.add_node(
            "resolve_destination",
            self._bounded_node(self._resolve_destination_node),
        )
        builder.add_node(
            "select_destination",
            self._bounded_node(self._select_destination_node),
        )
        builder.add_node("reserve_generate", self._bounded_node(self._reserve_generate_node))
        builder.add_node("generate_brief", self._bounded_node(self._generate_brief_node))
        builder.add_edge(START, "project_mobility")
        builder.add_edge("project_mobility", "reserve_origin")
        builder.add_edge("reserve_origin", "resolve_origin")
        builder.add_conditional_edges(
            "resolve_origin",
            self._origin_target,
            {
                "select_origin": "select_origin",
                "reserve_destination": "reserve_destination",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "select_origin",
            self._selected_origin_target,
            {
                "reserve_origin": "reserve_origin",
                "select_origin": "select_origin",
                "reserve_destination": "reserve_destination",
            },
        )
        builder.add_edge("reserve_destination", "resolve_destination")
        builder.add_conditional_edges(
            "resolve_destination",
            self._destination_target,
            {
                "select_destination": "select_destination",
                "reserve_generate": "reserve_generate",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "select_destination",
            self._selected_destination_target,
            {
                "reserve_destination": "reserve_destination",
                "select_destination": "select_destination",
                "reserve_generate": "reserve_generate",
            },
        )
        builder.add_edge("reserve_generate", "generate_brief")
        builder.add_edge("generate_brief", END)
        return builder.compile()

    async def _route_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        graph_steps, run_graph_steps, trajectory, run_trajectory = self._advance(
            state, "route", runtime.context
        )
        trip = TripContext.model_validate(state.get("trip_context", {}))
        mobility = MobilityContext.model_validate(state.get("mobility_context", {}))
        facility = FacilityContext.model_validate(state.get("facility_context", {}))
        pending_data = state.get("pending_clarification")
        pending = (
            PendingClarification.model_validate(pending_data) if pending_data is not None else None
        )
        routed = self._router.route(
            state["current_input"],
            trip=trip,
            mobility=mobility,
            facility=facility,
            pending=pending,
        )
        plan = self._policy.build_plan(routed.decision)
        clarification = self._clarification_for(routed.decision)
        if (
            pending is not None
            and pending.kind == ClarificationKind.MCP_CLARIFICATION
            and pending.intent != AgentIntent.FACILITY_STATUS
            and routed.trip == trip
            and routed.mobility == mobility
        ):
            # A yes/no answer is unsafe when the MCP question cannot be mapped to a slot.
            clarification = pending
        return AgentState(
            current_input=routed.normalized_input,
            intent_decision=routed.decision.model_dump(mode="json"),
            execution_plan=plan.model_dump(mode="json"),
            trip_context=routed.trip.model_dump(mode="json"),
            mobility_context=routed.mobility.model_dump(mode="json"),
            pending_clarification=(
                clarification.model_dump(mode="json") if clarification is not None else None
            ),
            origin_resolution=None,
            destination_resolution=None,
            facility_query=None,
            mcp_profile=None,
            projection_assumptions=[],
            final_outcome=None,
            external_error=None,
            graph_steps=graph_steps,
            trajectory=trajectory,
            run_graph_steps=run_graph_steps,
            run_trajectory=run_trajectory,
        )

    async def _clarify_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        pending = PendingClarification.model_validate(state["pending_clarification"])
        resumed = self._interrupt(pending.model_dump(mode="json"))
        graph_steps, run_graph_steps, trajectory, run_trajectory = self._advance(
            state, "clarify", runtime.context
        )
        if isinstance(resumed, str):
            if not resumed.strip():
                raise InvalidQuestionError()
            return AgentState(
                current_input=normalize_input(resumed),
                graph_steps=graph_steps,
                trajectory=trajectory,
                run_graph_steps=run_graph_steps,
                run_trajectory=run_trajectory,
            )

        try:
            structured = FieldsClarificationAnswer.model_validate(resumed)
        except ValidationError as exc:
            raise AgentPolicyViolationError(
                context={"reason": "structured_clarification_resume_invalid"}
            ) from exc

        values = {item.input_id: item.value for item in structured.values}
        expected_fields = {
            field
            for prompt in pending.prompts
            for field in prompt.fields
            if field
            in {
                "origin",
                "destination",
                "can_use_stairs",
                "can_use_escalator",
            }
        }
        if (
            pending.kind != ClarificationKind.REQUIRED_FIELDS
            or len(values) != len(structured.values)
            or set(values) != expected_fields
        ):
            raise AgentPolicyViolationError(
                context={"reason": "structured_clarification_contract_mismatch"}
            )
        unknown_fields = tuple(
            input_id
            for input_id, value in values.items()
            if isinstance(value, TriStateClarificationAnswerValue) and value.value == "unknown"
        )
        if unknown_fields:
            labels = {
                "can_use_stairs": "계단 이용 가능 여부",
                "can_use_escalator": "에스컬레이터 이용 가능 여부",
            }
            unverified = [labels.get(field, "필수 이동 조건") for field in unknown_fields]
            outcome = AnswerOutcome(
                source=AnswerSource.AGENT,
                status=ResponseStatus.FAILED,
                intent=QuestionIntent.TRIP_ACCESSIBILITY,
                user_message=(
                    "필수 이동 조건을 확인할 수 없어 이동 가능성을 판단하지 못했습니다. "
                    "조건을 확인한 뒤 새 조회를 시작해 주세요."
                ),
                evidence=AnswerEvidence(unverified_parts=unverified),
            )
            return AgentState(
                current_input="필수 이동 조건 미확인",
                pending_clarification=None,
                final_outcome=outcome.model_dump(mode="json"),
                graph_steps=graph_steps,
                trajectory=trajectory,
                run_graph_steps=run_graph_steps,
                run_trajectory=run_trajectory,
            )

        trip = TripContext.model_validate(state.get("trip_context", {}))
        mobility = MobilityContext.model_validate(state.get("mobility_context", {}))
        trip_update: dict[str, str | None] = {}
        mobility_update: dict[str, MobilityFlag] = {}
        for input_id, value in values.items():
            if isinstance(value, TextClarificationAnswerValue) and input_id == "origin":
                trip_update.update(
                    origin_query=value.value,
                    origin_station=(
                        trip.origin_station if value.value == trip.origin_query else None
                    ),
                    origin_line=trip.origin_line if value.value == trip.origin_query else None,
                )
            elif isinstance(value, TextClarificationAnswerValue) and input_id == "destination":
                trip_update.update(
                    destination_query=value.value,
                    destination_station=(
                        trip.destination_station if value.value == trip.destination_query else None
                    ),
                    destination_line=(
                        trip.destination_line if value.value == trip.destination_query else None
                    ),
                )
            elif isinstance(value, TriStateClarificationAnswerValue) and input_id in {
                "can_use_stairs",
                "can_use_escalator",
            }:
                mobility_update[input_id] = MobilityFlag.known(
                    value.value == "yes",
                    SlotSource.USER_CONFIRMED,
                )
            else:
                raise AgentPolicyViolationError(
                    context={"reason": "structured_clarification_field_unsupported"}
                )

        return AgentState(
            current_input="구조화된 추가 조건 확인",
            trip_context=(
                trip.model_copy(update=trip_update).model_dump(mode="json")
                if trip_update
                else trip.model_dump(mode="json")
            ),
            mobility_context=(
                mobility.with_flags(**mobility_update).model_dump(mode="json")
                if mobility_update
                else mobility.model_dump(mode="json")
            ),
            graph_steps=graph_steps,
            trajectory=trajectory,
            run_graph_steps=run_graph_steps,
            run_trajectory=run_trajectory,
        )

    async def _project_mobility_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        graph_steps, run_graph_steps, trajectory, run_trajectory = self._advance(
            state, "trip.project_mobility", runtime.context
        )
        mobility = MobilityContext.model_validate(state["mobility_context"])
        try:
            projection = project_mobility_context(mobility)
        except ValueError as exc:
            raise AgentPolicyViolationError(context={"reason": "unsafe_projection"}) from exc
        return AgentState(
            mcp_profile=projection.profile.model_dump(mode="json"),
            projection_assumptions=list(projection.current_turn_assumptions),
            graph_steps=graph_steps,
            trajectory=trajectory,
            run_graph_steps=run_graph_steps,
            run_trajectory=run_trajectory,
        )

    async def _prepare_facility_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        graph_steps, run_graph_steps, trajectory, run_trajectory = self._advance(
            state, "facility.prepare", runtime.context
        )
        if (
            McpCapability.FACILITY_QUESTION
            not in runtime.context.gateway.contract_snapshot.capabilities
        ):
            outcome = AnswerOutcome(
                source=AnswerSource.AGENT,
                status=ResponseStatus.FAILED,
                intent=QuestionIntent.FACILITY_STATUS,
                user_message=(
                    "현재 연결된 이동 정보 서비스는 역 시설 조회 계약을 제공하지 않습니다."
                ),
                evidence=AnswerEvidence(),
            )
            return AgentState(
                final_outcome=outcome.model_dump(mode="json"),
                pending_clarification=None,
                facility_query=None,
                graph_steps=graph_steps,
                trajectory=trajectory,
                run_graph_steps=run_graph_steps,
                run_trajectory=run_trajectory,
            )
        decision = IntentDecision.model_validate(state["intent_decision"])
        facility = FacilityContext.model_validate(state.get("facility_context", {}))
        query = _facility_tool_query(
            state["current_input"],
            facility=facility,
            decision=decision,
        )
        if len(query) > self._settings.mcp_tool_input_max_chars:
            raise QuestionTooLongError(
                context={
                    "max_chars": self._settings.mcp_tool_input_max_chars,
                    "actual_chars": len(query),
                    "target": "facility_question",
                }
            )
        return AgentState(
            facility_query=query,
            graph_steps=graph_steps,
            trajectory=trajectory,
            run_graph_steps=run_graph_steps,
            run_trajectory=run_trajectory,
        )

    async def _reserve_facility_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        return self._reserve_tool_node(
            state,
            capability=ToolCapability.ANSWER_FACILITY_QUESTION,
            node="facility.reserve_answer",
            context=runtime.context,
        )

    async def _answer_facility_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        graph_steps, run_graph_steps, trajectory, run_trajectory = self._advance(
            state, "facility.answer", runtime.context
        )
        query = state.get("facility_query")
        if not isinstance(query, str) or not query:
            raise AgentPolicyViolationError(context={"reason": "facility_query_missing"})
        try:
            result = await self._execute_tool(
                state,
                runtime=runtime,
                capability=ToolCapability.ANSWER_FACILITY_QUESTION,
                operation=lambda: runtime.context.gateway.answer_facility_question(query),
                summarize=_answer_facility_question_tool_summary,
            )
        except (
            McpUnavailableError,
            McpContractError,
            McpDependencyDegradedError,
            McpToolExecutionError,
            McpTimeoutError,
            McpResponseValidationError,
        ) as exc:
            return AgentState(
                external_error={"code": exc.code},
                facility_query=None,
                graph_steps=graph_steps,
                trajectory=trajectory,
                run_graph_steps=run_graph_steps,
                run_trajectory=run_trajectory,
            )
        decision = IntentDecision.model_validate(state["intent_decision"])
        previous = FacilityContext.model_validate(state.get("facility_context", {}))
        facility = _facility_context_from_result(
            result,
            previous=previous,
            decision=decision,
            current_input=state["current_input"],
        )
        outcome = AnswerOutcome.from_mcp_result(result)
        pending = _facility_pending_clarification(result, outcome=outcome)
        return AgentState(
            facility_context=facility.model_dump(mode="json"),
            facility_query=None,
            final_outcome=outcome.model_dump(mode="json"),
            pending_clarification=(
                pending.model_dump(mode="json") if pending is not None else None
            ),
            graph_steps=graph_steps,
            trajectory=trajectory,
            run_graph_steps=run_graph_steps,
            run_trajectory=run_trajectory,
        )

    async def _resolve_origin_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        return await self._resolve_station_node(
            state,
            runtime=runtime,
            field="origin",
        )

    async def _reserve_origin_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        return self._reserve_tool_node(
            state,
            capability=ToolCapability.RESOLVE_STATION,
            node="trip.reserve_origin",
            context=runtime.context,
        )

    async def _resolve_destination_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        return await self._resolve_station_node(
            state,
            runtime=runtime,
            field="destination",
        )

    async def _reserve_destination_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        return self._reserve_tool_node(
            state,
            capability=ToolCapability.RESOLVE_STATION,
            node="trip.reserve_destination",
            context=runtime.context,
        )

    async def _reserve_generate_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        return self._reserve_tool_node(
            state,
            capability=ToolCapability.GENERATE_ACCESSIBILITY_BRIEF,
            node="trip.reserve_generate",
            context=runtime.context,
        )

    def _reserve_tool_node(
        self,
        state: AgentState,
        *,
        capability: ToolCapability,
        node: str,
        context: RuntimeContext,
    ) -> AgentState:
        graph_steps, run_graph_steps, trajectory, run_trajectory = self._advance(
            state, node, context
        )
        tool_calls, ledger, parallel, run_tool_calls, run_tool_ledger = self._reserve_tool(
            state, capability, context
        )
        return AgentState(
            tool_calls=tool_calls,
            tool_ledger=ledger,
            max_parallel_tools_observed=parallel,
            graph_steps=graph_steps,
            trajectory=trajectory,
            run_graph_steps=run_graph_steps,
            run_tool_calls=run_tool_calls,
            run_tool_ledger=run_tool_ledger,
            run_trajectory=run_trajectory,
        )

    async def _execute_tool[ResultT](
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
        capability: ToolCapability,
        operation: Callable[[], Awaitable[ResultT]],
        summarize: Callable[[ResultT], ToolExecutionSummary],
    ) -> ResultT:
        context = runtime.context
        writer = context.run_ledger
        correlation = context.correlation
        if writer is None or correlation is None:
            return await operation()

        ordinal = state.get("run_tool_calls")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise AgentPolicyViolationError(context={"reason": "tool_execution_ordinal_invalid"})
        logical_call_id = _logical_tool_call_id(correlation, capability, ordinal)
        # In the durable path this is deliberately the queue claim/recovery attempt,
        # not a per-capability retry counter. It keeps immutable Tool records unique
        # when a checkpoint resumes the same logical call. The legacy path stays at 1.
        tool_attempt = (
            context.execution_lease.generation if context.execution_lease is not None else 1
        )
        execution_id = f"{logical_call_id}:attempt:{tool_attempt}"
        handle = await writer.append_tool_started(
            correlation,
            owner_id=str(context.owner_id),
            execution_id=execution_id,
            logical_call_id=logical_call_id,
            attempt=tool_attempt,
            tool=capability,
            execution_lease=context.execution_lease,
        )
        started_ns = monotonic_ns()
        try:
            result = await operation()
            summary = summarize(result)
        except asyncio.CancelledError:
            await writer.append_tool_terminal(
                handle,
                owner_id=str(context.owner_id),
                transition=ToolTransition.FAILED,
                duration_ms=_elapsed_milliseconds(started_ns),
                error_code=ToolFailureCode.EXECUTION_CANCELLED,
                execution_lease=context.execution_lease,
            )
            raise
        except Exception as exc:
            transition, error_code = _tool_failure(exc)
            await writer.append_tool_terminal(
                handle,
                owner_id=str(context.owner_id),
                transition=transition,
                duration_ms=_elapsed_milliseconds(started_ns),
                error_code=error_code,
                execution_lease=context.execution_lease,
            )
            raise

        await writer.append_tool_terminal(
            handle,
            owner_id=str(context.owner_id),
            transition=ToolTransition.COMPLETED,
            duration_ms=_elapsed_milliseconds(started_ns),
            summary=summary,
            execution_lease=context.execution_lease,
        )
        return result

    async def _resolve_station_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
        field: Literal["origin", "destination"],
    ) -> AgentState:
        node_name = f"trip.resolve_{field}"
        graph_steps, run_graph_steps, trajectory, run_trajectory = self._advance(
            state, node_name, runtime.context
        )
        trip = TripContext.model_validate(state["trip_context"])
        query = trip.origin_query if field == "origin" else trip.destination_query
        line = trip.origin_line if field == "origin" else trip.destination_line
        if query is None:
            raise AgentPolicyViolationError(context={"reason": f"{field}_query_missing"})
        lookup_query = _line_aware_station(query, line)
        try:
            result = await self._execute_tool(
                state,
                runtime=runtime,
                capability=ToolCapability.RESOLVE_STATION,
                operation=lambda: runtime.context.gateway.resolve_station(lookup_query),
                summarize=_resolve_station_tool_summary,
            )
        except (
            McpUnavailableError,
            McpContractError,
            McpDependencyDegradedError,
            McpToolExecutionError,
            McpTimeoutError,
            McpResponseValidationError,
        ) as exc:
            return AgentState(
                external_error={"code": exc.code},
                graph_steps=graph_steps,
                trajectory=trajectory,
                run_graph_steps=run_graph_steps,
                run_trajectory=run_trajectory,
            )
        compact = _compact_resolution(result)
        updates: dict[str, object] = {
            f"{field}_resolution": compact,
            "graph_steps": graph_steps,
            "trajectory": trajectory,
            "run_graph_steps": run_graph_steps,
            "run_trajectory": run_trajectory,
        }
        if result.matched_station is not None and not result.needs_clarification:
            station_name = result.matched_station.station_name
            trip_update = (
                {
                    "origin_station": station_name,
                    "origin_line": result.matched_station.line,
                }
                if field == "origin"
                else {
                    "destination_station": station_name,
                    "destination_line": result.matched_station.line,
                }
            )
            updates["trip_context"] = trip.model_copy(update=trip_update).model_dump(mode="json")
        return cast(AgentState, updates)

    async def _select_origin_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        return self._select_station_node(state, runtime=runtime, field="origin")

    async def _select_destination_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        return self._select_station_node(state, runtime=runtime, field="destination")

    def _select_station_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
        field: Literal["origin", "destination"],
    ) -> AgentState:
        resolution_data = (
            state["origin_resolution"] if field == "origin" else state["destination_resolution"]
        )
        resolution = StationResolutionResult.model_validate(resolution_data)
        candidates = tuple(_station_option(candidate) for candidate in resolution.candidates)
        field_label = "출발역" if field == "origin" else "도착역"
        message = resolution.clarification_message or f"{field_label}을 다시 확인해 주세요."
        pending = PendingClarification(
            kind=ClarificationKind.STATION_SELECTION,
            intent=AgentIntent.TRIP_ACCESSIBILITY,
            fields=(field,),
            questions=(message,),
            prompts=(PendingPrompt(id=field, question=message, fields=(field,)),),
            message=message,
            candidates=candidates,
        )
        resumed = self._interrupt(pending.model_dump(mode="json"))
        selected_from_structured: dict[str, str | None] | None = None
        if isinstance(resumed, str):
            if not resumed.strip():
                raise InvalidQuestionError()
            selection = normalize_input(resumed)
        else:
            try:
                structured = FieldsClarificationAnswer.model_validate(resumed)
            except ValidationError as exc:
                raise AgentPolicyViolationError(
                    context={"reason": "structured_station_resume_invalid"}
                ) from exc
            if len(structured.values) != 1:
                raise AgentPolicyViolationError(
                    context={"reason": "structured_station_answer_count"}
                )
            submitted = structured.values[0]
            if submitted.input_id != field or not isinstance(
                submitted.value, ChoiceClarificationAnswerValue
            ):
                raise AgentPolicyViolationError(
                    context={"reason": "structured_station_answer_type"}
                )
            token = submitted.value.value
            raw_index = token.removeprefix("option-")
            if not token.startswith("option-") or not raw_index.isdigit():
                raise AgentPolicyViolationError(
                    context={"reason": "structured_station_option_invalid"}
                )
            option_index = int(raw_index) - 1
            if option_index < 0 or option_index >= len(candidates):
                raise AgentPolicyViolationError(
                    context={"reason": "structured_station_option_invalid"}
                )
            selected_from_structured = candidates[option_index]
            selection = str(selected_from_structured["label"])
        trip = TripContext.model_validate(state["trip_context"])
        graph_steps, run_graph_steps, trajectory, run_trajectory = self._advance(
            state, f"trip.select_{field}", runtime.context
        )

        if not candidates:
            update = (
                {"origin_query": selection, "origin_station": None, "origin_line": None}
                if field == "origin"
                else {
                    "destination_query": selection,
                    "destination_station": None,
                    "destination_line": None,
                }
            )
            if field == "origin":
                return AgentState(
                    trip_context=trip.model_copy(update=update).model_dump(mode="json"),
                    origin_resolution=None,
                    graph_steps=graph_steps,
                    trajectory=trajectory,
                    run_graph_steps=run_graph_steps,
                    run_trajectory=run_trajectory,
                )
            return AgentState(
                trip_context=trip.model_copy(update=update).model_dump(mode="json"),
                destination_resolution=None,
                graph_steps=graph_steps,
                trajectory=trajectory,
                run_graph_steps=run_graph_steps,
                run_trajectory=run_trajectory,
            )

        selected = selected_from_structured or _match_candidate(selection, candidates)
        if selected is None:
            invalid_resolution = resolution.model_copy(
                update={"clarification_message": "제시된 역 후보 중 하나를 선택해 주세요."}
            ).model_dump(mode="json")
            if field == "origin":
                return AgentState(
                    origin_resolution=invalid_resolution,
                    graph_steps=graph_steps,
                    trajectory=trajectory,
                    run_graph_steps=run_graph_steps,
                    run_trajectory=run_trajectory,
                )
            return AgentState(
                destination_resolution=invalid_resolution,
                graph_steps=graph_steps,
                trajectory=trajectory,
                run_graph_steps=run_graph_steps,
                run_trajectory=run_trajectory,
            )

        station_name = selected["station_name"]
        update = (
            {"origin_station": station_name, "origin_line": selected["line"]}
            if field == "origin"
            else {"destination_station": station_name, "destination_line": selected["line"]}
        )
        return AgentState(
            trip_context=trip.model_copy(update=update).model_dump(mode="json"),
            graph_steps=graph_steps,
            trajectory=trajectory,
            run_graph_steps=run_graph_steps,
            run_trajectory=run_trajectory,
        )

    async def _generate_brief_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        graph_steps, run_graph_steps, trajectory, run_trajectory = self._advance(
            state, "trip.generate_brief", runtime.context
        )
        trip = TripContext.model_validate(state["trip_context"])
        origin_station = trip.origin_station
        destination_station = trip.destination_station
        if origin_station is None or destination_station is None:
            raise AgentPolicyViolationError(context={"reason": "resolved_trip_missing"})
        profile = MobilityProfile.model_validate(state["mcp_profile"])
        try:
            result = await self._execute_tool(
                state,
                runtime=runtime,
                capability=ToolCapability.GENERATE_ACCESSIBILITY_BRIEF,
                operation=lambda: runtime.context.gateway.generate_accessibility_brief(
                    _line_aware_station(origin_station, trip.origin_line),
                    _line_aware_station(destination_station, trip.destination_line),
                    profile,
                ),
                summarize=_generate_accessibility_brief_tool_summary,
            )
        except (
            McpUnavailableError,
            McpContractError,
            McpDependencyDegradedError,
            McpToolExecutionError,
            McpTimeoutError,
            McpResponseValidationError,
        ) as exc:
            return AgentState(
                external_error={"code": exc.code},
                graph_steps=graph_steps,
                trajectory=trajectory,
                run_graph_steps=run_graph_steps,
                run_trajectory=run_trajectory,
            )
        outcome = _outcome_from_trip_result(result)
        pending_questions = tuple(outcome.questions) or (
            "이동 조건을 조금 더 구체적으로 알려주세요.",
        )
        pending = (
            PendingClarification(
                kind=ClarificationKind.MCP_CLARIFICATION,
                intent=AgentIntent.TRIP_ACCESSIBILITY,
                fields=_mcp_clarification_fields(list(pending_questions)),
                questions=pending_questions,
                prompts=tuple(
                    PendingPrompt(
                        id=f"mcp-{index}",
                        question=question,
                        fields=_mcp_clarification_fields([question]) or ("other",),
                    )
                    for index, question in enumerate(pending_questions, start=1)
                ),
                message=outcome.user_message,
            )
            if outcome.status == ResponseStatus.NEEDS_CLARIFICATION
            else None
        )
        return AgentState(
            final_outcome=outcome.model_dump(mode="json"),
            pending_clarification=(
                pending.model_dump(mode="json") if pending is not None else None
            ),
            graph_steps=graph_steps,
            trajectory=trajectory,
            run_graph_steps=run_graph_steps,
            run_trajectory=run_trajectory,
        )

    def _route_target(
        self,
        state: AgentState,
    ) -> Literal["clarify", "trip_workflow", "facility_workflow"]:
        if state.get("pending_clarification") is not None:
            return "clarify"
        plan = ExecutionPlan.model_validate(state["execution_plan"])
        if plan.playbook == Playbook.STRUCTURED_TRIP_V1:
            return "trip_workflow"
        if plan.playbook == Playbook.STRUCTURED_FACILITY_V1:
            return "facility_workflow"
        raise AgentPolicyViolationError(context={"reason": "playbook_without_target"})

    @staticmethod
    def _clarification_target(state: AgentState) -> Literal["route", "end"]:
        return "end" if state.get("final_outcome") is not None else "route"

    def _trip_workflow_target(
        self,
        state: AgentState,
    ) -> Literal["raise_external_error", "end"]:
        return "raise_external_error" if state.get("external_error") is not None else "end"

    def _facility_workflow_target(
        self,
        state: AgentState,
    ) -> Literal["raise_external_error", "end"]:
        return "raise_external_error" if state.get("external_error") is not None else "end"

    def _prepared_facility_target(
        self,
        state: AgentState,
    ) -> Literal["reserve_facility", "end"]:
        return "end" if state.get("final_outcome") is not None else "reserve_facility"

    async def _raise_external_error_node(
        self,
        state: AgentState,
        *,
        runtime: Runtime[RuntimeContext],
    ) -> AgentState:
        del runtime
        error = state.get("external_error") or {}
        error_type = _MCP_ERROR_BY_CODE.get(error.get("code", ""))
        if error_type is None:
            raise AgentPolicyViolationError(context={"reason": "external_error_invalid"})
        raise error_type()

    def _origin_target(
        self, state: AgentState
    ) -> Literal["select_origin", "reserve_destination", "end"]:
        if state.get("external_error") is not None:
            return "end"
        trip = TripContext.model_validate(state["trip_context"])
        return "reserve_destination" if trip.origin_station is not None else "select_origin"

    def _selected_origin_target(
        self, state: AgentState
    ) -> Literal["reserve_origin", "select_origin", "reserve_destination"]:
        trip = TripContext.model_validate(state["trip_context"])
        if trip.origin_station is not None:
            return "reserve_destination"
        return "reserve_origin" if state.get("origin_resolution") is None else "select_origin"

    def _destination_target(
        self, state: AgentState
    ) -> Literal["select_destination", "reserve_generate", "end"]:
        if state.get("external_error") is not None:
            return "end"
        trip = TripContext.model_validate(state["trip_context"])
        return "reserve_generate" if trip.destination_station is not None else "select_destination"

    def _selected_destination_target(
        self, state: AgentState
    ) -> Literal["reserve_destination", "select_destination", "reserve_generate"]:
        trip = TripContext.model_validate(state["trip_context"])
        if trip.destination_station is not None:
            return "reserve_generate"
        return (
            "reserve_destination"
            if state.get("destination_resolution") is None
            else "select_destination"
        )

    def _clarification_for(
        self,
        decision: IntentDecision,
    ) -> PendingClarification | None:
        if decision.intent == AgentIntent.FACILITY_STATUS:
            return None
        if decision.intent != AgentIntent.TRIP_ACCESSIBILITY:
            question = "출발역, 도착역과 필요한 이동 조건을 알려주세요."
            return PendingClarification(
                kind=ClarificationKind.UNSUPPORTED_INTENT,
                intent=decision.intent,
                fields=("intent",),
                questions=(question,),
                prompts=(PendingPrompt(id="intent", question=question, fields=("intent",)),),
                message="현재 질문의 이동 요청 범위를 명확히 확인해야 합니다.",
            )
        if not decision.missing_fields:
            return None

        prompts: list[PendingPrompt] = []
        missing = set(decision.missing_fields)
        if "origin" in missing:
            prompts.append(
                PendingPrompt(
                    id="origin",
                    question="출발역은 어디인가요?",
                    fields=("origin",),
                )
            )
        if "destination" in missing:
            prompts.append(
                PendingPrompt(
                    id="destination",
                    question="도착역은 어디인가요?",
                    fields=("destination",),
                )
            )
        mobility_fields = missing & {"can_use_stairs", "can_use_escalator"}
        if mobility_fields == {"can_use_stairs", "can_use_escalator"}:
            prompts.append(
                PendingPrompt(
                    id="stairs-and-escalator",
                    question="계단과 에스컬레이터를 각각 이용할 수 있나요?",
                    fields=("can_use_stairs", "can_use_escalator"),
                )
            )
        elif "can_use_stairs" in mobility_fields:
            prompts.append(
                PendingPrompt(
                    id="can-use-stairs",
                    question="계단을 이용할 수 있나요?",
                    fields=("can_use_stairs",),
                )
            )
        elif "can_use_escalator" in mobility_fields:
            prompts.append(
                PendingPrompt(
                    id="can-use-escalator",
                    question="에스컬레이터를 이용할 수 있나요?",
                    fields=("can_use_escalator",),
                )
            )
        return PendingClarification(
            kind=ClarificationKind.REQUIRED_FIELDS,
            intent=AgentIntent.TRIP_ACCESSIBILITY,
            fields=decision.missing_fields,
            questions=tuple(prompt.question for prompt in prompts),
            prompts=tuple(prompts),
            message="안전한 이동 경로 확인을 위해 정보가 더 필요합니다.",
        )

    def _advance(
        self,
        state: AgentState,
        node: str,
        context: RuntimeContext,
    ) -> tuple[int, int, list[str], list[str]]:
        graph_steps = state.get("graph_steps", 0) + 1
        if graph_steps > context.limits.max_graph_steps:
            raise AgentBudgetExceededError(
                context={"budget": "graph_steps", "limit": context.limits.max_graph_steps}
            )
        run_graph_steps = state.get("run_graph_steps", 0) + 1
        run_limit = context.limits.max_graph_steps * (context.limits.max_clarification_cycles + 1)
        if run_graph_steps > run_limit:
            raise AgentBudgetExceededError(
                context={"budget": "run_graph_steps", "limit": run_limit}
            )
        return (
            graph_steps,
            run_graph_steps,
            [*state.get("trajectory", []), node],
            [*state.get("run_trajectory", []), node],
        )

    def _reserve_tool(
        self,
        state: AgentState,
        capability: ToolCapability,
        context: RuntimeContext,
    ) -> tuple[int, list[str], int, int, list[str]]:
        plan = ExecutionPlan.model_validate(state["execution_plan"])
        ledger = state.get("tool_ledger", [])
        self._policy.ensure_allowed(plan, capability, ledger=ledger)
        tool_calls = state.get("tool_calls", 0) + 1
        limit = min(plan.max_tool_calls, context.limits.max_tool_calls)
        if tool_calls > limit:
            raise AgentBudgetExceededError(context={"budget": "tool_calls", "limit": limit})
        run_tool_calls = state.get("run_tool_calls", 0) + 1
        run_limit = limit * (context.limits.max_clarification_cycles + 1)
        if run_tool_calls > run_limit:
            raise AgentBudgetExceededError(context={"budget": "run_tool_calls", "limit": run_limit})
        parallel = 1
        parallel_limit = min(plan.max_parallel_tools, context.limits.max_parallel_tools)
        if parallel > parallel_limit:
            raise AgentBudgetExceededError(
                context={"budget": "parallel_tools", "limit": parallel_limit}
            )
        return (
            tool_calls,
            [*ledger, capability.value],
            max(state.get("max_parallel_tools_observed", 0), parallel),
            run_tool_calls,
            [*state.get("run_tool_ledger", []), capability.value],
        )

    def _config(
        self,
        thread_id: str,
        owner_id: OwnerId,
        *,
        correlation: RunExecutionCorrelation | None = None,
        checkpoint_cursor: str | None = None,
        execution_lease: ExecutionLease | None = None,
    ) -> RunnableConfig:
        configurable: dict[str, object] = {
            "thread_id": self._checkpoint_thread_id(thread_id, owner_id),
        }
        if checkpoint_cursor is not None:
            configurable["checkpoint_id"] = checkpoint_cursor
        config: RunnableConfig = {
            "configurable": {
                **configurable,
            },
            # Nested subgraphs consume framework supersteps in addition to our node ledger.
            "recursion_limit": self._settings.agent_max_graph_steps * 4,
        }
        if correlation is not None:
            config["metadata"] = {
                "bfm_run_id": correlation.run_id,
                "bfm_request_id": correlation.request_id,
                "bfm_attempt": correlation.attempt,
            }
        if execution_lease is not None:
            metadata = dict(config.get("metadata", {}))
            metadata.update(
                {
                    "bfm_execution_job_id": execution_lease.job_id,
                    "bfm_lease_generation": execution_lease.generation,
                }
            )
            config["metadata"] = metadata
        return attach_execution_lease(config, execution_lease)

    def _runtime_context_for(
        self,
        thread_id: str,
        *,
        owner_id: OwnerId,
        correlation: RunExecutionCorrelation | None,
        execution_lease: ExecutionLease | None,
    ) -> RuntimeContext:
        if correlation is not None and correlation.thread_id != thread_id:
            raise AgentPolicyViolationError(context={"reason": "run_correlation_thread_mismatch"})
        if execution_lease is not None and (
            correlation is None
            or execution_lease.correlation != correlation
            or execution_lease.owner_id != str(owner_id)
        ):
            raise AgentPolicyViolationError(context={"reason": "execution_lease_context_mismatch"})
        return RuntimeContext(
            gateway=self._gateway,
            limits=self._execution_limits,
            flow_version=self._settings.agent_flow_version,
            state_schema_version=self._settings.agent_state_schema_version,
            owner_id=owner_id,
            correlation=correlation,
            run_ledger=self._run_ledger,
            execution_lease=execution_lease,
        )

    @staticmethod
    def _checkpoint_thread_id(thread_id: str, owner_id: OwnerId) -> str:
        return f"{owner_id}:{thread_id}"

    def _to_turn(self, result: GraphOutput[AgentState]) -> GraphTurn:
        state = dict(result.value)
        usage = _run_usage_from_state(state)
        if result.interrupts:
            first = result.interrupts[0]
            pending = PendingClarification.model_validate(first.value)
            clarification = project_pending_clarification(
                pending,
                clarification_id=first.id,
            )
            intent = _public_question_intent(pending)
            outcome = AnswerOutcome(
                source=AnswerSource.AGENT,
                status=ResponseStatus.NEEDS_CLARIFICATION,
                intent=intent,
                user_message=pending.message,
                clarification_needed=True,
                clarification=clarification,
                questions=list(pending.questions),
                clarification_options=list(clarification.options),
                evidence=AnswerEvidence(),
            )
            return GraphTurn(
                status=GraphTurnStatus.INTERRUPTED,
                resume_mode=ResumeMode.COMMAND,
                outcome=outcome,
                context=project_conversation_context(
                    state,
                    intent=intent,
                    active_clarification=clarification,
                ),
                state=state,
                usage=usage,
                interrupt_id=first.id,
            )
        final_outcome = state.get("final_outcome")
        if final_outcome is None:
            raise AgentPolicyViolationError(context={"reason": "final_outcome_missing"})
        outcome = AnswerOutcome.model_validate(final_outcome)
        if outcome.status == ResponseStatus.NEEDS_CLARIFICATION:
            pending_data = state.get("pending_clarification")
            if pending_data is None:
                raise AgentPolicyViolationError(context={"reason": "pending_clarification_missing"})
            pending = PendingClarification.model_validate(pending_data)
            clarification_cycles = state.get("clarification_cycles", 0)
            if not isinstance(clarification_cycles, int):
                raise AgentPolicyViolationError(context={"reason": "clarification_cycles_invalid"})
            clarification = project_pending_clarification(
                pending,
                clarification_id=f"business-{clarification_cycles + 1}",
            )
            outcome = outcome.model_copy(
                update={
                    "clarification": clarification,
                    "clarification_options": list(clarification.options),
                }
            )
            return GraphTurn(
                status=GraphTurnStatus.INTERRUPTED,
                resume_mode=ResumeMode.NEW_TURN,
                outcome=outcome,
                context=project_conversation_context(
                    state,
                    intent=outcome.intent,
                    active_clarification=clarification,
                ),
                state=state,
                usage=usage,
            )
        return GraphTurn(
            status=GraphTurnStatus.COMPLETED,
            resume_mode=ResumeMode.NONE,
            outcome=outcome,
            context=project_conversation_context(
                state,
                intent=outcome.intent,
                active_clarification=None,
            ),
            state=state,
            usage=usage,
        )


def _logical_tool_call_id(
    correlation: RunExecutionCorrelation,
    capability: ToolCapability,
    ordinal: int,
) -> str:
    material = f"{correlation.run_id}\x1f{correlation.attempt}\x1f{capability.value}\x1f{ordinal}"
    digest = sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"tool:{correlation.attempt}:{capability.value}:{ordinal}:{digest}"


def _elapsed_milliseconds(started_ns: int) -> int:
    return max(0, (monotonic_ns() - started_ns) // 1_000_000)


def _checkpoint_cursor(config: RunnableConfig) -> str | None:
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    value = configurable.get("checkpoint_id")
    return value if isinstance(value, str) and value else None


def _checkpoint_correlation(
    metadata: Mapping[str, object] | None,
    *,
    thread_id: str,
) -> RunExecutionCorrelation | None:
    if metadata is None:
        return None
    values = (
        metadata.get("bfm_run_id"),
        metadata.get("bfm_request_id"),
        metadata.get("bfm_attempt"),
    )
    if values == (None, None, None):
        return None
    run_id, request_id, attempt = values
    if (
        not isinstance(run_id, str)
        or not isinstance(request_id, str)
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
    ):
        raise AgentStateIncompatibleError(
            context={"reason": "checkpoint_execution_metadata_invalid"}
        )
    try:
        return RunExecutionCorrelation(
            thread_id=thread_id,
            run_id=run_id,
            request_id=request_id,
            attempt=attempt,
        )
    except ValidationError:
        raise AgentStateIncompatibleError(
            context={"reason": "checkpoint_execution_metadata_invalid"}
        ) from None


def _checkpoint_execution_provenance(
    metadata: Mapping[str, object] | None,
) -> tuple[str | None, int | None]:
    if metadata is None:
        return None, None
    job_id = metadata.get("bfm_execution_job_id")
    generation = metadata.get("bfm_lease_generation")
    if job_id is None and generation is None:
        return None, None
    if (
        not isinstance(job_id, str)
        or not job_id
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise AgentStateIncompatibleError(
            context={"reason": "checkpoint_execution_provenance_invalid"}
        )
    return job_id, generation


def _tool_failure(exc: Exception) -> tuple[ToolTransition, ToolFailureCode]:
    if isinstance(exc, McpTimeoutError):
        return ToolTransition.TIMEOUT, ToolFailureCode.MCP_TIMEOUT
    if isinstance(exc, McpUnavailableError):
        return ToolTransition.FAILED, ToolFailureCode.MCP_UNAVAILABLE
    if isinstance(exc, McpContractError):
        return ToolTransition.FAILED, ToolFailureCode.MCP_CONTRACT_ERROR
    if isinstance(exc, McpDependencyDegradedError):
        return ToolTransition.FAILED, ToolFailureCode.MCP_DEGRADED
    if isinstance(exc, McpToolExecutionError):
        return ToolTransition.FAILED, ToolFailureCode.MCP_TOOL_EXECUTION_ERROR
    if isinstance(exc, McpResponseValidationError):
        return ToolTransition.FAILED, ToolFailureCode.MCP_RESPONSE_VALIDATION_ERROR
    return ToolTransition.FAILED, ToolFailureCode.UNEXPECTED_ERROR


def _resolve_station_tool_summary(
    result: StationResolutionResult,
) -> ToolExecutionSummary:
    return ResolveStationToolSummary(
        matched=result.matched_station is not None,
        candidate_count=min(len(result.candidates), 20),
        clarification_required=result.needs_clarification,
    )


def _generate_accessibility_brief_tool_summary(
    result: AccessibilityResult,
) -> ToolExecutionSummary:
    return GenerateAccessibilityBriefToolSummary(
        response_status=result.status,
        clarification_required=result.clarification_needed,
        evidence_count=min(len(result.evidence_sources), 100),
        limitation_count=min(len(result.limitations), 100),
    )


def _answer_facility_question_tool_summary(
    result: AccessibilityQuestionResult,
) -> ToolExecutionSummary:
    evidence_count = (
        min(len(result.facility_result.evidence_sources), 100)
        if result.facility_result is not None
        else 0
    )
    return AnswerFacilityQuestionToolSummary(
        response_status=result.status,
        clarification_required=result.clarification_needed,
        evidence_count=evidence_count,
    )


def _run_usage_from_state(state: Mapping[str, object]) -> RunUsage:
    if "run_usage" not in state:
        return RunUsage()
    payload = state["run_usage"]
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != RUN_USAGE_SCHEMA_VERSION
    ):
        raise AgentStateIncompatibleError(
            context={"expected_usage_schema_version": RUN_USAGE_SCHEMA_VERSION}
        )
    try:
        return RunUsage.model_validate(dict(payload))
    except ValidationError as exc:
        raise AgentStateIncompatibleError(
            context={"expected_usage_schema_version": RUN_USAGE_SCHEMA_VERSION}
        ) from exc


def _compact_resolution(result: StationResolutionResult) -> dict[str, object]:
    return {
        "query": result.query,
        "matched_station": (
            result.matched_station.model_dump(mode="json")
            if result.matched_station is not None
            else None
        ),
        "candidates": [candidate.model_dump(mode="json") for candidate in result.candidates],
        "needs_clarification": result.needs_clarification,
        "clarification_message": result.clarification_message,
    }


def _station_option(station: Station) -> dict[str, str | None]:
    line_label = _normalize_line_label(station.line)
    label = (
        f"{station.station_name} ({line_label})" if line_label is not None else station.station_name
    )
    return {
        "station_name": station.station_name,
        "line": station.line,
        "label": label,
    }


def _line_aware_station(station_name: str, line: str | None) -> str:
    line_label = _normalize_line_label(line)
    return f"{line_label} {station_name}" if line_label is not None else station_name


def _normalize_line_label(line: str | None) -> str | None:
    if line is None:
        return None
    normalized = line.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return f"{int(normalized)}호선"
    return normalized


def _match_candidate(
    selection: str,
    candidates: tuple[dict[str, str | None], ...],
) -> dict[str, str | None] | None:
    normalized = selection.removesuffix("역").strip()
    label_matches = [
        candidate
        for candidate in candidates
        if normalize_input(str(candidate["label"])).removesuffix("역") == normalized
    ]
    if len(label_matches) == 1:
        return label_matches[0]
    name_matches = [
        candidate
        for candidate in candidates
        if str(candidate["station_name"]).removesuffix("역") == normalized
    ]
    return name_matches[0] if len(name_matches) == 1 else None


def _outcome_from_trip_result(result: AccessibilityResult) -> AnswerOutcome:
    return AnswerOutcome(
        source=AnswerSource.MCP,
        status=result.status,
        intent=QuestionIntent.TRIP_ACCESSIBILITY,
        user_message=result.user_message,
        clarification_needed=(
            result.clarification_needed or result.status == ResponseStatus.NEEDS_CLARIFICATION
        ),
        questions=result.questions,
        available_partial_info=result.available_partial_info,
        evidence=AnswerEvidence(
            risk_level=result.risk_level,
            confidence_level=result.confidence_level,
            failed_sources=project_failed_source_summaries(result.failed_sources),
            limitations=result.limitations,
            unverified_parts=result.unverified_parts,
            last_checked_at=result.last_checked_at,
        ),
        presentation=(
            None
            if result.status == ResponseStatus.NEEDS_CLARIFICATION
            else trip_answer_presentation(result)
        ),
    )


def _mcp_clarification_fields(questions: list[str]) -> tuple[str, ...]:
    text = " ".join(questions)
    fields: list[str] = []
    if "계단" in text:
        fields.append("can_use_stairs")
    if "에스컬레이터" in text:
        fields.append("can_use_escalator")
    if "엘리베이터" in text or "승강기" in text:
        fields.append("need_elevator_only")
    if "휠체어" in text and "휠체어 충전" not in text:
        fields.append("wheelchair")
    if "유모차" in text:
        fields.append("stroller")
    if "지팡이" in text or "보행기" in text or "워커" in text:
        fields.append("cane_or_walker")
    if "장애인 화장실" in text:
        fields.append("need_accessible_restroom")
    if "휠체어 충전" in text:
        fields.append("need_wheelchair_charger")
    unique_fields = tuple(dict.fromkeys(fields))
    selection_markers = ("어떤", "무엇", "중 ", "선택", "알려")
    unsafe_polarity_markers = ("못", "없", "않", "아니", "불가", "어려", "피하", "제외")
    if (
        len(unique_fields) != 1
        or any(marker in text for marker in selection_markers)
        or any(marker in text for marker in unsafe_polarity_markers)
    ):
        return ("mcp_clarification",)
    field = unique_fields[0]
    return unique_fields if _is_positive_yes_no_question(field, text) else ("mcp_clarification",)


def _is_positive_yes_no_question(field: str, text: str) -> bool:
    if not any(marker in text for marker in ("나요", "습니까", "맞죠")):
        return False
    if field == "can_use_stairs":
        return "계단" in text and any(marker in text for marker in ("할 수", "가능"))
    if field == "can_use_escalator":
        return "에스컬레이터" in text and any(marker in text for marker in ("할 수", "가능"))
    if field == "need_elevator_only":
        return ("엘리베이터만" in text or "승강기만" in text) and any(
            marker in text for marker in ("이용", "사용", "타야", "필요", "해야")
        )
    if field == "wheelchair":
        return "휠체어" in text and any(marker in text for marker in ("사용", "이용", "타"))
    if field == "stroller":
        return "유모차" in text and any(marker in text for marker in ("사용", "이용"))
    if field == "cane_or_walker":
        return any(marker in text for marker in ("지팡이", "보행기", "워커")) and any(
            marker in text for marker in ("사용", "이용")
        )
    if field == "need_accessible_restroom":
        return "장애인 화장실" in text and "필요" in text
    if field == "need_wheelchair_charger":
        return "휠체어 충전" in text and "필요" in text
    return False


def _facility_pending_clarification(
    result: AccessibilityQuestionResult,
    *,
    outcome: AnswerOutcome,
) -> PendingClarification | None:
    if outcome.status != ResponseStatus.NEEDS_CLARIFICATION:
        return None
    reason_codes = tuple(result.parsed.missing_fields)
    fields = tuple(
        dict.fromkeys(
            {
                "facility_type": "facility_types",
                "accessible_restroom_confirmation": "accessible_restroom_confirmation",
            }.get(reason, reason)
            for reason in reason_codes
        )
    ) or ("other",)
    questions = tuple(outcome.questions)
    prompt_fields: tuple[tuple[str, ...], ...]
    if len(questions) == len(fields):
        prompt_fields = tuple((field,) for field in fields)
    else:
        prompt_fields = tuple(fields for _ in questions)
    return PendingClarification(
        kind=ClarificationKind.MCP_CLARIFICATION,
        intent=AgentIntent.FACILITY_STATUS,
        fields=fields,
        reason_codes=reason_codes,
        questions=questions,
        prompts=tuple(
            PendingPrompt(
                id=f"facility-{index}",
                question=question,
                fields=prompt_fields[index - 1],
            )
            for index, question in enumerate(questions, start=1)
        ),
        message=outcome.user_message,
    )


def _facility_context_from_result(
    result: AccessibilityQuestionResult,
    *,
    previous: FacilityContext,
    decision: IntentDecision,
    current_input: str,
) -> FacilityContext:
    parsed = result.parsed
    nested = result.facility_result
    signal_codes = set(decision.signal_codes)
    target_query = parsed.target_station or previous.target_query
    target_station = nested.station_name if nested is not None else previous.target_station
    target_line = (
        nested.line
        if nested is not None and nested.line is not None
        else parsed.target_line or previous.target_line
    )
    target_source = previous.target_source
    if parsed.target_station is not None:
        target_source = (
            SlotSource.THREAD_CONTEXT
            if "facility_context" in signal_codes
            and parsed.target_station in {previous.target_query, previous.target_station}
            else SlotSource.USER_CONFIRMED
            if decision.source == DecisionSource.RESUME
            else SlotSource.USER_EXPLICIT
        )

    facility_types = tuple(parsed.facility_types) or previous.facility_types
    facility_types_source = previous.facility_types_source
    has_explicit_type = any(term in current_input for term in ("엘리베이터", "승강기", "화장실"))
    if parsed.facility_types:
        facility_types_source = (
            SlotSource.USER_CONFIRMED
            if decision.source == DecisionSource.RESUME
            else SlotSource.USER_EXPLICIT
            if has_explicit_type
            else SlotSource.THREAD_CONTEXT
        )
    return FacilityContext(
        target_query=target_query,
        target_station=target_station,
        target_line=target_line,
        target_source=target_source,
        facility_types=facility_types,
        facility_types_source=facility_types_source,
        question_kind=(
            nested.question_kind
            if nested is not None
            else parsed.facility_question_kind or previous.question_kind
        ),
        accessible_restroom_confirmation_pending=(
            "accessible_restroom_confirmation" in parsed.missing_fields
        ),
    )


def _facility_tool_query(
    current_input: str,
    *,
    facility: FacilityContext,
    decision: IntentDecision,
) -> str:
    uses_context = (
        decision.source == DecisionSource.RESUME or "facility_context" in decision.signal_codes
    )
    if not uses_context:
        return current_input
    parts: list[str] = []
    target = facility.target_station or facility.target_query
    if target is not None:
        parts.append(_line_aware_station(target, facility.target_line))
    has_explicit_type = any(term in current_input for term in ("엘리베이터", "승강기", "화장실"))
    if facility.accessible_restroom_confirmation_pending and current_input in {
        "네",
        "예",
        "맞아요",
        "맞습니다",
    }:
        parts.append("장애인 화장실")
    elif not has_explicit_type:
        parts.extend(
            {
                FacilityType.ELEVATOR: "엘리베이터",
                FacilityType.WHEELCHAIR_LIFT: "휠체어리프트",
                FacilityType.ACCESSIBLE_RESTROOM: "장애인 화장실",
            }.get(facility_type, facility_type.value)
            for facility_type in facility.facility_types
        )
    if facility.question_kind is not None:
        parts.append(
            {
                FacilityQuestionKind.STATUS: "운행 상태",
                FacilityQuestionKind.LOCATION: "위치",
                FacilityQuestionKind.EXISTENCE: "설치 여부",
                FacilityQuestionKind.OVERVIEW: "시설 정보",
            }[facility.question_kind]
        )
    parts.append(current_input)
    return normalize_input(" ".join(dict.fromkeys(parts)))


def _public_question_intent(pending: PendingClarification) -> QuestionIntent:
    if pending.intent == AgentIntent.TRIP_ACCESSIBILITY:
        return QuestionIntent.TRIP_ACCESSIBILITY
    if pending.intent == AgentIntent.FACILITY_STATUS:
        return QuestionIntent.FACILITY_STATUS
    if pending.intent == AgentIntent.ALTERNATIVE_REQUEST:
        return QuestionIntent.ALTERNATIVE_REQUEST
    return (
        QuestionIntent.TRIP_ACCESSIBILITY
        if pending.kind != ClarificationKind.UNSUPPORTED_INTENT
        else QuestionIntent.UNKNOWN
    )
