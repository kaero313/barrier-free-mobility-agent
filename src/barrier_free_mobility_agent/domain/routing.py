from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AgentIntent(StrEnum):
    TRIP_ACCESSIBILITY = "trip_accessibility"
    FACILITY_STATUS = "facility_status"
    ALTERNATIVE_REQUEST = "alternative_request"
    DIRECT_HELP = "direct_help"
    UNKNOWN = "unknown"


class DecisionSource(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"
    RESUME = "resume"


class ToolCapability(StrEnum):
    RESOLVE_STATION = "resolve_station"
    GENERATE_ACCESSIBILITY_BRIEF = "generate_accessibility_brief"
    ANSWER_FACILITY_QUESTION = "answer_facility_question"


class Playbook(StrEnum):
    STRUCTURED_TRIP_V1 = "structured_trip.v1"
    STRUCTURED_FACILITY_V1 = "structured_facility.v1"
    CLARIFY_V1 = "clarify.v1"


_REQUIRED_CAPABILITIES_BY_INTENT: dict[AgentIntent, tuple[ToolCapability, ...]] = {
    AgentIntent.TRIP_ACCESSIBILITY: (
        ToolCapability.RESOLVE_STATION,
        ToolCapability.GENERATE_ACCESSIBILITY_BRIEF,
    ),
    AgentIntent.FACILITY_STATUS: (ToolCapability.ANSWER_FACILITY_QUESTION,),
    # "다른 경로"는 같은 경로 조회에 다른 경로 비교만 더해 MCP에 묻는다.
    AgentIntent.ALTERNATIVE_REQUEST: (
        ToolCapability.RESOLVE_STATION,
        ToolCapability.GENERATE_ACCESSIBILITY_BRIEF,
    ),
}


def required_capabilities_for_intent(
    intent: AgentIntent,
) -> tuple[ToolCapability, ...]:
    """Return the single code-owned intent-to-capability policy mapping."""

    return _REQUIRED_CAPABILITIES_BY_INTENT.get(intent, ())


class IntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: AgentIntent
    source: DecisionSource
    confidence: float = Field(ge=0, le=1)
    required_capabilities: tuple[ToolCapability, ...] = ()
    missing_fields: tuple[str, ...] = ()
    signal_codes: tuple[str, ...] = ()


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    playbook: Playbook
    allowed_tools: tuple[ToolCapability, ...]
    tool_limits: dict[ToolCapability, int]
    max_tool_calls: int = Field(ge=0)
    max_model_calls: int = Field(ge=0)
    max_parallel_tools: int = Field(ge=1)
