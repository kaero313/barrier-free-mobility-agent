from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from barrier_free_mobility_agent.application.model_boundary import (
    FallbackTokenCounter,
    ModelFailure,
    ModelFailureKind,
    ModelInvocation,
    ModelMessage,
    ModelMessageRole,
    ModelProfile,
    ModelPurpose,
    ModelPurposeAdmission,
    ModelPurposePolicy,
    ModelResult,
    ModelResultIdentityError,
    ProviderRequestOption,
    PurposeDecisionReason,
    StructuredModel,
    TokenCount,
    TokenCounter,
    TokenCountKind,
    canonical_token_blocks,
    validate_result_identity,
)
from barrier_free_mobility_agent.domain.usage import ModelUsage, UsageSource


class _RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Literal["trip_accessibility", "unknown"]
    confidence: float


class _FakeStructuredModel:
    version = "fake-exact-v1"

    async def count_input[ResultT: BaseModel](
        self,
        invocation: ModelInvocation[ResultT],
    ) -> TokenCount:
        return await FallbackTokenCounter().count_input(invocation)

    async def invoke_structured[ResultT: BaseModel](
        self,
        invocation: ModelInvocation[ResultT],
        *,
        preflight: TokenCount | None = None,
    ) -> ModelFailure:
        if invocation.max_output_tokens < 1:
            raise AssertionError("The invocation contract already forbids this value.")
        return ModelFailure(kind=ModelFailureKind.TIMEOUT, safe_error_code="MODEL_TIMEOUT")


def _invocation(**updates: object) -> ModelInvocation[_RouteDecision]:
    values: dict[str, object] = {
        "purpose": ModelPurpose.AMBIGUOUS_ROUTING,
        "prompt_version": "route.v1",
        "context_policy_version": "context.v1",
        "profile": ModelProfile(
            provider_alias="fake-provider",
            model_alias="router-small",
            provider_contract_version="fake-provider-contract.v1",
            context_window_tokens=8192,
            hard_max_output_tokens=512,
            reasoning_tokens_in_output_cap=True,
        ),
        "system_policy": "허용된 IntentDecision 필드만 반환하세요.",
        "messages": (
            ModelMessage(role=ModelMessageRole.USER, content="서울역에서 거기까지 갈 수 있어?"),
        ),
        "response_model": _RouteDecision,
        "provider_options": (
            ProviderRequestOption(name="reasoning_effort", value="low"),
            ProviderRequestOption(name="temperature", value=0.0),
        ),
        "max_output_tokens": 256,
    }
    values.update(updates)
    return ModelInvocation[_RouteDecision](**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fallback_v1_counts_utf8_bytes_and_logical_block_overhead() -> None:
    invocation = _invocation()
    blocks = canonical_token_blocks(invocation)
    canonical_bytes = sum(len(block) for block in blocks)

    count = await FallbackTokenCounter().count_input(invocation)

    assert len(blocks) == 4  # system, user, response schema, provider options
    assert count.input_tokens == (canonical_bytes * 5 + 3) // 4 + 32 * len(blocks)
    assert count.canonical_bytes == canonical_bytes
    assert count.logical_blocks == len(blocks)
    assert count.counter_version == "fallback-v1"
    assert count.kind == TokenCountKind.ESTIMATED
    assert await FallbackTokenCounter().count_input(invocation) == count


def test_model_invocation_rejects_noncanonical_or_unsafe_provider_contracts() -> None:
    profile_without_contract = _invocation().profile.model_dump()
    profile_without_contract.pop("provider_contract_version")
    with pytest.raises(ValidationError):
        ModelProfile.model_validate(profile_without_contract)

    with pytest.raises(ValidationError, match="hard output cap"):
        _invocation(max_output_tokens=513)

    with pytest.raises(ValidationError, match="canonical name order"):
        _invocation(
            provider_options=(
                ProviderRequestOption(name="temperature", value=0.0),
                ProviderRequestOption(name="reasoning_effort", value="low"),
            )
        )

    with pytest.raises(ValidationError, match="unique"):
        _invocation(
            provider_options=(
                ProviderRequestOption(name="temperature", value=0.0),
                ProviderRequestOption(name="temperature", value=0.1),
            )
        )

    with pytest.raises(ValidationError):
        ProviderRequestOption(name="system_hint", value="x" * 2_001)
    with pytest.raises(ValidationError):
        _invocation(
            provider_options=tuple(
                ProviderRequestOption(name=f"option-{index:02}", value=index) for index in range(33)
            )
        )


def test_model_boundary_models_are_strict_frozen_and_do_not_accept_raw_failure_data() -> None:
    invocation = _invocation()

    with pytest.raises(ValidationError):
        ModelInvocation[_RouteDecision].model_validate(
            {
                **invocation.model_dump(),
                "response_model": _RouteDecision,
                "unexpected": "not allowed",
            }
        )
    with pytest.raises(ValidationError):
        ModelFailure.model_validate(
            {
                "kind": ModelFailureKind.INVALID_STRUCTURED_OUTPUT,
                "safe_error_code": "MODEL_OUTPUT_INVALID",
                "raw_output": "서울역과 사용자의 이동 조건",
            }
        )
    with pytest.raises(ValidationError):
        ModelFailure(
            kind=ModelFailureKind.PROVIDER_ERROR,
            safe_error_code="unsafe free-form message",
        )
    with pytest.raises(ValidationError):
        invocation.max_output_tokens = 1  # type: ignore[misc]


def test_structured_result_preserves_validated_value_and_normalized_usage() -> None:
    usage = ModelUsage(
        input_tokens=120,
        output_tokens=20,
        total_tokens=140,
        source=UsageSource.REPORTED,
    )

    result = ModelResult[_RouteDecision](
        value=_RouteDecision(intent="trip_accessibility", confidence=0.95),
        usage=usage,
        provider_alias="fake-provider",
        model_alias="router-small",
        provider_contract_version="fake-provider-contract.v1",
        prompt_version="route.v1",
    )

    assert result.value.intent == "trip_accessibility"
    assert result.usage == usage
    assert validate_result_identity(_invocation(), result) is result

    result_without_contract = result.model_dump()
    result_without_contract.pop("provider_contract_version")
    with pytest.raises(ValidationError):
        ModelResult[_RouteDecision].model_validate(result_without_contract)

    mismatched = result.model_copy(update={"model_alias": "different-model"})
    with pytest.raises(ModelResultIdentityError):
        validate_result_identity(_invocation(), mismatched)

    contract_mismatched = result.model_copy(
        update={"provider_contract_version": "fake-provider-contract.v2"}
    )
    with pytest.raises(ModelResultIdentityError):
        validate_result_identity(_invocation(), contract_mismatched)

    failure = ModelFailure(
        kind=ModelFailureKind.TIMEOUT,
        safe_error_code="MODEL_TIMEOUT",
    )
    assert validate_result_identity(_invocation(), failure) is failure


@pytest.mark.asyncio
async def test_counter_and_structured_model_protocols_share_the_invocation_contract() -> None:
    counter: TokenCounter = FallbackTokenCounter()
    model: StructuredModel = _FakeStructuredModel()

    assert (await counter.count_input(_invocation())).input_tokens > 0
    assert model.version == "fake-exact-v1"


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({}, PurposeDecisionReason.ALLOWED),
        (
            {"active_pending_clarification": True},
            PurposeDecisionReason.ACTIVE_PENDING_CLARIFICATION,
        ),
        ({"active_nested_interrupt": True}, PurposeDecisionReason.ACTIVE_NESTED_INTERRUPT),
        ({"fresh_turn": False}, PurposeDecisionReason.NOT_FRESH_TURN),
        ({"route_is_ambiguous": False}, PurposeDecisionReason.ROUTE_NOT_AMBIGUOUS),
        ({"model_calls_this_turn": 1}, PurposeDecisionReason.CALL_LIMIT_EXHAUSTED),
    ],
)
def test_model_purpose_policy_only_allows_fresh_ambiguous_routing(
    updates: dict[str, object],
    expected: PurposeDecisionReason,
) -> None:
    values: dict[str, object] = {
        "purpose": ModelPurpose.AMBIGUOUS_ROUTING,
        "fresh_turn": True,
        "route_is_ambiguous": True,
        "active_pending_clarification": False,
        "active_nested_interrupt": False,
        "model_calls_this_turn": 0,
    }
    values.update(updates)
    admission = ModelPurposeAdmission(**values)  # type: ignore[arg-type]

    decision = ModelPurposePolicy().evaluate(admission)

    assert decision.reason == expected
    assert decision.allowed is (expected == PurposeDecisionReason.ALLOWED)
    assert decision.max_calls_per_turn == 1
    assert decision.policy_version == "ambiguous-routing.v1"


def test_model_purpose_admission_rejects_coerced_boolean_values() -> None:
    with pytest.raises(ValidationError):
        ModelPurposeAdmission.model_validate(
            {
                "purpose": ModelPurpose.AMBIGUOUS_ROUTING,
                "fresh_turn": "true",
                "route_is_ambiguous": True,
                "active_pending_clarification": False,
                "active_nested_interrupt": False,
                "model_calls_this_turn": 0,
            }
        )
