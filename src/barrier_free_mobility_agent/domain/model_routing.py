from __future__ import annotations

import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from barrier_free_mobility_agent.domain.routing import AgentIntent, ToolCapability

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
_MAX_CURRENT_INPUT_CHARS = 20_000

ModelRoutingIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=_IDENTIFIER_PATTERN),
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class StructuredIntentDecision(_StrictFrozenModel):
    """The complete and deliberately narrow output contract for route classification."""

    schema_version: Literal["1"]
    intent: AgentIntent
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("intent", mode="before")
    @classmethod
    def parse_json_intent(cls, value: object) -> object:
        """Accept the exact JSON enum representation without relaxing other fields."""

        if type(value) is str:
            try:
                return AgentIntent(value)
            except ValueError:
                return value
        return value


class ModelContextEnvelope(_StrictFrozenModel):
    """A bounded, minimal projection assembled only for one model invocation.

    The current input is user data; every retained-context field around it is a
    value-free presence flag. The envelope does not accept conversation history,
    station names copied from retained state, or raw MCP/provider payloads.
    """

    schema_version: Literal["1"]
    context_policy_version: ModelRoutingIdentifier
    current_input: str = Field(
        min_length=1,
        max_length=_MAX_CURRENT_INPUT_CHARS,
        repr=False,
    )
    has_active_trip: bool
    has_origin: bool
    has_destination: bool
    has_previous_destination: bool
    has_facility_target: bool
    can_use_stairs_known: bool
    can_use_escalator_known: bool
    available_capabilities: tuple[ToolCapability, ...] = Field(
        min_length=0,
        max_length=len(ToolCapability),
    )

    @field_validator("current_input")
    @classmethod
    def require_normalized_input(cls, value: str) -> str:
        normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
        if value != normalized:
            raise ValueError("current_input must already be normalized")
        return value

    @model_validator(mode="after")
    def validate_context_flags_and_capabilities(self) -> ModelContextEnvelope:
        if (self.has_origin or self.has_destination) and not self.has_active_trip:
            raise ValueError("trip slots require an active trip")

        canonical = tuple(sorted(set(self.available_capabilities), key=lambda item: item.value))
        if self.available_capabilities != canonical:
            raise ValueError("available_capabilities must be unique and canonically ordered")
        return self


MODEL_CONTEXT_MAX_CURRENT_INPUT_CHARS = _MAX_CURRENT_INPUT_CHARS
