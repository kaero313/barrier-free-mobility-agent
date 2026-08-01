from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from barrier_free_mobility_agent.domain.usage import ModelUsage

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
Identifier = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=_IDENTIFIER_PATTERN),
]
ProviderOptionString = Annotated[str, Field(min_length=1, max_length=2_000)]
ProviderOptionValue = ProviderOptionString | int | float | bool | None


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ModelPurpose(StrEnum):
    AMBIGUOUS_ROUTING = "ambiguous_routing"


class StructuredOutputMode(StrEnum):
    JSON_SCHEMA = "json_schema"


class ModelMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ModelFailureKind(StrEnum):
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    INVALID_STRUCTURED_OUTPUT = "invalid_structured_output"
    PROVIDER_ERROR = "provider_error"


class TokenCountKind(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"


class PurposeDecisionReason(StrEnum):
    ALLOWED = "allowed"
    ACTIVE_PENDING_CLARIFICATION = "active_pending_clarification"
    ACTIVE_NESTED_INTERRUPT = "active_nested_interrupt"
    NOT_FRESH_TURN = "not_fresh_turn"
    ROUTE_NOT_AMBIGUOUS = "route_not_ambiguous"
    CALL_LIMIT_EXHAUSTED = "call_limit_exhausted"


class ModelProfile(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    provider_alias: Identifier
    model_alias: Identifier
    provider_contract_version: Identifier
    context_window_tokens: int = Field(gt=0, le=2_000_000)
    hard_max_output_tokens: int = Field(gt=0, le=1_000_000)
    reasoning_tokens_in_output_cap: bool

    @model_validator(mode="after")
    def validate_output_window(self) -> ModelProfile:
        if self.hard_max_output_tokens >= self.context_window_tokens:
            raise ValueError("The model output cap must be smaller than its context window.")
        return self


class ModelMessage(StrictFrozenModel):
    role: ModelMessageRole
    content: str = Field(min_length=1, max_length=100_000, repr=False)


class ProviderRequestOption(StrictFrozenModel):
    """One immutable, token-bearing provider option included in preflight counting."""

    name: Identifier
    value: ProviderOptionValue


class ModelInvocation[ResponseT: BaseModel](StrictFrozenModel):
    """The complete immutable input shared by token counting and provider invocation."""

    schema_version: Literal["1"] = "1"
    purpose: ModelPurpose
    prompt_version: Identifier
    context_policy_version: Identifier
    profile: ModelProfile
    system_policy: str = Field(min_length=1, max_length=100_000, repr=False)
    messages: tuple[ModelMessage, ...] = Field(min_length=1, max_length=100)
    response_model: type[ResponseT]
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.JSON_SCHEMA
    provider_options: tuple[ProviderRequestOption, ...] = Field(default=(), max_length=32)
    max_output_tokens: int = Field(gt=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_provider_contract(self) -> ModelInvocation[ResponseT]:
        if self.max_output_tokens > self.profile.hard_max_output_tokens:
            raise ValueError("Invocation output tokens exceed the provider hard output cap.")
        option_names = [option.name for option in self.provider_options]
        if len(option_names) != len(set(option_names)):
            raise ValueError("Provider option names must be unique.")
        if option_names != sorted(option_names):
            raise ValueError("Provider options must use canonical name order.")
        return self


class TokenCount(StrictFrozenModel):
    input_tokens: int = Field(ge=0)
    kind: TokenCountKind
    counter_version: Identifier
    canonical_bytes: int = Field(ge=0)
    logical_blocks: int = Field(ge=1)


class ModelResult[ResponseT: BaseModel](StrictFrozenModel):
    value: ResponseT
    usage: ModelUsage
    provider_alias: Identifier
    model_alias: Identifier
    provider_contract_version: Identifier
    prompt_version: Identifier


class ModelFailure(StrictFrozenModel):
    kind: ModelFailureKind
    usage: ModelUsage | None = None
    safe_error_code: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )


type ModelInvocationResult[ResultT: BaseModel] = ModelResult[ResultT] | ModelFailure


class ModelResultIdentityError(ValueError):
    """A provider adapter returned metadata that does not match its invocation."""


def validate_result_identity[ResultT: BaseModel](
    invocation: ModelInvocation[ResultT],
    result: ModelInvocationResult[ResultT],
) -> ModelInvocationResult[ResultT]:
    if isinstance(result, ModelFailure):
        return result
    expected = (
        invocation.profile.provider_alias,
        invocation.profile.model_alias,
        invocation.profile.provider_contract_version,
        invocation.prompt_version,
    )
    actual = (
        result.provider_alias,
        result.model_alias,
        result.provider_contract_version,
        result.prompt_version,
    )
    if actual != expected:
        raise ModelResultIdentityError("model result identity does not match the invocation")
    return result


class TokenCounter(Protocol):
    @property
    def version(self) -> str: ...

    async def count_input[ResultT: BaseModel](
        self,
        invocation: ModelInvocation[ResultT],
    ) -> TokenCount: ...


class StructuredModel(TokenCounter, Protocol):
    async def invoke_structured[ResultT: BaseModel](
        self,
        invocation: ModelInvocation[ResultT],
        *,
        preflight: TokenCount | None = None,
    ) -> ModelInvocationResult[ResultT]: ...


class FallbackTokenCounter:
    """Provider-independent local/test admission estimator from ADR-0006."""

    version = "fallback-v1"
    _OVERHEAD_PER_BLOCK = 32

    async def count_input[ResultT: BaseModel](
        self,
        invocation: ModelInvocation[ResultT],
    ) -> TokenCount:
        blocks = canonical_token_blocks(invocation)
        canonical_bytes = sum(len(block) for block in blocks)
        # ceil(bytes * 1.25) without floating-point rounding.
        estimated_content = (canonical_bytes * 5 + 3) // 4
        return TokenCount(
            input_tokens=estimated_content + self._OVERHEAD_PER_BLOCK * len(blocks),
            kind=TokenCountKind.ESTIMATED,
            counter_version=self.version,
            canonical_bytes=canonical_bytes,
            logical_blocks=len(blocks),
        )


def canonical_token_blocks[ResultT: BaseModel](
    invocation: ModelInvocation[ResultT],
) -> tuple[bytes, ...]:
    """Return every canonical input block counted by the fallback estimator."""

    blocks = [
        _canonical_json_bytes({"content": invocation.system_policy, "role": "system"}),
        *(
            _canonical_json_bytes(message.model_dump(mode="json"))
            for message in invocation.messages
        ),
        _canonical_json_bytes(
            {
                "response_schema": invocation.response_model.model_json_schema(mode="validation"),
                "structured_output_mode": invocation.structured_output_mode.value,
            }
        ),
    ]
    if invocation.provider_options:
        blocks.append(
            _canonical_json_bytes(
                {
                    "provider_options": [
                        option.model_dump(mode="json") for option in invocation.provider_options
                    ]
                }
            )
        )
    return tuple(blocks)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ModelPurposeAdmission(StrictFrozenModel):
    purpose: ModelPurpose
    fresh_turn: bool
    route_is_ambiguous: bool
    active_pending_clarification: bool
    active_nested_interrupt: bool
    model_calls_this_turn: int = Field(ge=0)


class ModelPurposeDecision(StrictFrozenModel):
    allowed: bool
    reason: PurposeDecisionReason
    purpose: ModelPurpose
    policy_version: Identifier
    max_calls_per_turn: Literal[1] = 1

    @model_validator(mode="after")
    def validate_allowed_reason(self) -> ModelPurposeDecision:
        if self.allowed != (self.reason == PurposeDecisionReason.ALLOWED):
            raise ValueError("Only the allowed reason can produce an allowed decision.")
        return self


class ModelPurposePolicy(StrictFrozenModel):
    """Pure Stage A admission policy; graph wiring is intentionally deferred."""

    schema_version: Literal["1"] = "1"
    version: Identifier = "ambiguous-routing.v1"
    max_calls_per_turn: Literal[1] = 1

    def evaluate(self, admission: ModelPurposeAdmission) -> ModelPurposeDecision:
        reason = self._reason(admission)
        return ModelPurposeDecision(
            allowed=reason == PurposeDecisionReason.ALLOWED,
            reason=reason,
            purpose=admission.purpose,
            policy_version=self.version,
            max_calls_per_turn=self.max_calls_per_turn,
        )

    def _reason(self, admission: ModelPurposeAdmission) -> PurposeDecisionReason:
        if admission.active_pending_clarification:
            return PurposeDecisionReason.ACTIVE_PENDING_CLARIFICATION
        if admission.active_nested_interrupt:
            return PurposeDecisionReason.ACTIVE_NESTED_INTERRUPT
        if not admission.fresh_turn:
            return PurposeDecisionReason.NOT_FRESH_TURN
        if not admission.route_is_ambiguous:
            return PurposeDecisionReason.ROUTE_NOT_AMBIGUOUS
        if admission.model_calls_this_turn >= self.max_calls_per_turn:
            return PurposeDecisionReason.CALL_LIMIT_EXHAUSTED
        return PurposeDecisionReason.ALLOWED
