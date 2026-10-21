from __future__ import annotations

from barrier_free_mobility_agent.domain.errors import AgentPolicyViolationError
from barrier_free_mobility_agent.domain.routing import (
    AgentIntent,
    ExecutionPlan,
    IntentDecision,
    Playbook,
    ToolCapability,
    required_capabilities_for_intent,
)


class ToolPolicy:
    def build_plan(self, decision: IntentDecision) -> ExecutionPlan:
        if decision.intent in {AgentIntent.TRIP_ACCESSIBILITY, AgentIntent.ALTERNATIVE_REQUEST}:
            return ExecutionPlan(
                playbook=Playbook.STRUCTURED_TRIP_V1,
                allowed_tools=required_capabilities_for_intent(decision.intent),
                tool_limits={
                    ToolCapability.RESOLVE_STATION: 4,
                    ToolCapability.GENERATE_ACCESSIBILITY_BRIEF: 1,
                },
                max_tool_calls=5,
                max_model_calls=0,
                max_parallel_tools=1,
            )
        if decision.intent == AgentIntent.FACILITY_STATUS:
            return ExecutionPlan(
                playbook=Playbook.STRUCTURED_FACILITY_V1,
                allowed_tools=required_capabilities_for_intent(decision.intent),
                tool_limits={ToolCapability.ANSWER_FACILITY_QUESTION: 1},
                max_tool_calls=1,
                max_model_calls=0,
                max_parallel_tools=1,
            )
        return ExecutionPlan(
            playbook=Playbook.CLARIFY_V1,
            allowed_tools=(),
            tool_limits={},
            max_tool_calls=0,
            max_model_calls=0,
            max_parallel_tools=1,
        )

    def ensure_allowed(
        self,
        plan: ExecutionPlan,
        capability: ToolCapability,
        *,
        ledger: list[str],
    ) -> None:
        if capability not in plan.allowed_tools:
            raise AgentPolicyViolationError(context={"capability": capability.value})
        used = sum(entry == capability.value for entry in ledger)
        limit = plan.tool_limits.get(capability, 0)
        if used >= limit:
            raise AgentPolicyViolationError(
                context={"capability": capability.value, "reason": "tool_limit"}
            )
