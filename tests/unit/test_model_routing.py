from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from barrier_free_mobility_agent.application.model_boundary import (
    FallbackTokenCounter,
    ModelProfile,
    ProviderRequestOption,
)
from barrier_free_mobility_agent.application.model_routing import (
    MODEL_ROUTING_MIN_CONFIDENCE,
    ModelRoutingInvocationAssembler,
    canonical_model_context,
    canonical_routing_invocation_bytes,
    prepare_routing_reservation_material,
    project_structured_intent,
)
from barrier_free_mobility_agent.domain.model_reservation import (
    ModelReservationFingerprintSigner,
    open_model_reservation,
    verify_model_reservation,
)
from barrier_free_mobility_agent.domain.model_routing import (
    MODEL_CONTEXT_MAX_CURRENT_INPUT_CHARS,
    ModelContextEnvelope,
    StructuredIntentDecision,
)
from barrier_free_mobility_agent.domain.routing import (
    AgentIntent,
    DecisionSource,
    ToolCapability,
)


def _profile() -> ModelProfile:
    return ModelProfile(
        provider_alias="recorded-provider",
        model_alias="router-small",
        provider_contract_version="recorded-provider-contract.v1",
        context_window_tokens=8_192,
        hard_max_output_tokens=256,
        reasoning_tokens_in_output_cap=True,
    )


def _assembler() -> ModelRoutingInvocationAssembler:
    return ModelRoutingInvocationAssembler(
        profile=_profile(),
        provider_options=(
            ProviderRequestOption(name="temperature", value=0.0),
            ProviderRequestOption(name="reasoning_effort", value="low"),
        ),
    )


def _assembly(**updates: object):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "current_input": "어디로 가야 편할까요?",
        "has_active_trip": True,
        "has_origin": True,
        "has_destination": False,
        "has_previous_destination": True,
        "has_facility_target": False,
        "can_use_stairs_known": False,
        "can_use_escalator_known": True,
        "available_capabilities": tuple(ToolCapability),
    }
    values.update(updates)
    return _assembler().assemble(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("intent", list(AgentIntent))
def test_structured_intent_contract_supports_every_agent_intent(intent: AgentIntent) -> None:
    decision = StructuredIntentDecision(schema_version="1", intent=intent, confidence=0.75)

    assert decision.intent is intent
    assert set(decision.model_dump()) == {"schema_version", "intent", "confidence"}


def test_structured_intent_accepts_exact_provider_json_but_rejects_coercion() -> None:
    decision = StructuredIntentDecision.model_validate(
        {
            "schema_version": "1",
            "intent": "trip_accessibility",
            "confidence": 0.8,
        }
    )

    assert decision.intent == AgentIntent.TRIP_ACCESSIBILITY
    with pytest.raises(ValidationError):
        StructuredIntentDecision.model_validate(
            {
                "schema_version": "1",
                "intent": "not-an-intent",
                "confidence": 0.8,
            }
        )
    with pytest.raises(ValidationError):
        StructuredIntentDecision.model_validate(
            {
                "schema_version": "1",
                "intent": "trip_accessibility",
                "confidence": "0.8",
            }
        )


def test_structured_intent_contract_is_strict_frozen_and_cannot_choose_tools() -> None:
    with pytest.raises(ValidationError):
        StructuredIntentDecision.model_validate(
            {
                "schema_version": "1",
                "intent": AgentIntent.TRIP_ACCESSIBILITY,
                "confidence": 0.9,
                "required_capabilities": [ToolCapability.RESOLVE_STATION],
            }
        )
    with pytest.raises(ValidationError):
        StructuredIntentDecision.model_validate(
            {
                "schema_version": "1",
                "intent": AgentIntent.UNKNOWN,
                "confidence": "0.9",
            }
        )
    with pytest.raises(ValidationError):
        StructuredIntentDecision(
            schema_version="1",
            intent=AgentIntent.UNKNOWN,
            confidence=1.01,
        )

    decision = StructuredIntentDecision(
        schema_version="1",
        intent=AgentIntent.UNKNOWN,
        confidence=0.5,
    )
    with pytest.raises(ValidationError):
        decision.confidence = 0.9  # type: ignore[misc]


def test_context_envelope_enforces_normalized_input_size_and_flag_bounds() -> None:
    assert len(
        _assembly(current_input="가" * MODEL_CONTEXT_MAX_CURRENT_INPUT_CHARS).envelope.current_input
    ) == (MODEL_CONTEXT_MAX_CURRENT_INPUT_CHARS)

    with pytest.raises(ValidationError, match="at most"):
        _assembly(current_input="가" * (MODEL_CONTEXT_MAX_CURRENT_INPUT_CHARS + 1))
    with pytest.raises(ValidationError, match="normalized"):
        _assembly(current_input="  정규화되지   않은 입력  ")
    with pytest.raises(ValidationError):
        _assembly(has_active_trip=False, has_origin=True)
    with pytest.raises(ValidationError):
        ModelContextEnvelope.model_validate(
            {
                **_assembly().envelope.model_dump(),
                "available_capabilities": (
                    ToolCapability.RESOLVE_STATION,
                    ToolCapability.RESOLVE_STATION,
                ),
            }
        )


def test_projector_recalculates_missing_fields_and_tools_from_code() -> None:
    assembly = _assembly()
    model_decision = StructuredIntentDecision(
        schema_version="1",
        intent=AgentIntent.TRIP_ACCESSIBILITY,
        confidence=0.83,
    )

    projected = project_structured_intent(model_decision, assembly.envelope)

    assert projected.source == DecisionSource.MODEL
    assert projected.required_capabilities == (
        ToolCapability.RESOLVE_STATION,
        ToolCapability.GENERATE_ACCESSIBILITY_BRIEF,
    )
    assert projected.missing_fields == ("destination", "can_use_stairs")
    assert projected.signal_codes == ("structured_model",)


def test_facility_intent_without_runtime_capability_fails_closed() -> None:
    assembly = _assembly(
        has_facility_target=True,
        available_capabilities=(
            ToolCapability.GENERATE_ACCESSIBILITY_BRIEF,
            ToolCapability.RESOLVE_STATION,
        ),
    )
    model_decision = StructuredIntentDecision(
        schema_version="1",
        intent=AgentIntent.FACILITY_STATUS,
        confidence=0.99,
    )

    projected = project_structured_intent(model_decision, assembly.envelope)

    assert projected.intent == AgentIntent.UNKNOWN
    assert projected.source == DecisionSource.MODEL
    assert projected.required_capabilities == ()
    assert projected.signal_codes == (
        "structured_model",
        "model_capability_unavailable",
    )


@pytest.mark.parametrize(
    ("confidence", "expected_intent"),
    [
        (0.0, AgentIntent.UNKNOWN),
        (MODEL_ROUTING_MIN_CONFIDENCE - 0.01, AgentIntent.UNKNOWN),
        (MODEL_ROUTING_MIN_CONFIDENCE, AgentIntent.TRIP_ACCESSIBILITY),
        (1.0, AgentIntent.TRIP_ACCESSIBILITY),
    ],
)
def test_projector_fails_closed_below_the_versioned_confidence_threshold(
    confidence: float,
    expected_intent: AgentIntent,
) -> None:
    assembly = _assembly()
    projected = project_structured_intent(
        StructuredIntentDecision(
            schema_version="1",
            intent=AgentIntent.TRIP_ACCESSIBILITY,
            confidence=confidence,
        ),
        assembly.envelope,
    )

    assert projected.intent == expected_intent
    if expected_intent == AgentIntent.UNKNOWN:
        assert projected.required_capabilities == ()
        assert projected.missing_fields == ()
        assert projected.signal_codes == (
            "structured_model",
            "model_confidence_below_threshold",
        )


def test_invocation_assembly_is_canonical_versioned_and_deterministic() -> None:
    first = _assembly(
        available_capabilities=(
            ToolCapability.RESOLVE_STATION,
            ToolCapability.ANSWER_FACILITY_QUESTION,
            ToolCapability.GENERATE_ACCESSIBILITY_BRIEF,
        )
    )
    second = _assembly(
        available_capabilities=(
            ToolCapability.GENERATE_ACCESSIBILITY_BRIEF,
            ToolCapability.RESOLVE_STATION,
            ToolCapability.ANSWER_FACILITY_QUESTION,
        )
    )

    assert first == second
    assert first.invocation.prompt_version == "ambiguous-routing.v1"
    assert first.invocation.context_policy_version == "ambiguous-routing-context.v1"
    assert first.invocation.profile == _profile()
    assert tuple(option.name for option in first.invocation.provider_options) == (
        "reasoning_effort",
        "temperature",
    )
    assert first.invocation.response_model is StructuredIntentDecision
    assert first.invocation.messages[0].content == canonical_model_context(first.envelope)
    assert json.loads(first.invocation.messages[0].content) == first.envelope.model_dump(
        mode="json",
        exclude={"available_capabilities"},
    )
    assert all(
        capability.value not in first.invocation.messages[0].content
        for capability in ToolCapability
    )
    assert " " not in first.invocation.messages[0].content.replace(
        first.envelope.current_input,
        "",
    )
    assert canonical_routing_invocation_bytes(
        first.invocation
    ) == canonical_routing_invocation_bytes(second.invocation)
    changed = _assembly(current_input="다른 이동 질문")
    assert canonical_routing_invocation_bytes(
        first.invocation
    ) != canonical_routing_invocation_bytes(changed.invocation)
    changed_output_cap = ModelRoutingInvocationAssembler(
        profile=_profile(),
        provider_options=_assembler().provider_options,
        max_output_tokens=64,
    ).assemble(**first.envelope.model_dump(exclude={"schema_version", "context_policy_version"}))
    assert canonical_routing_invocation_bytes(
        first.invocation
    ) != canonical_routing_invocation_bytes(changed_output_cap.invocation)
    changed_profile = first.invocation.model_copy(
        update={
            "profile": first.invocation.profile.model_copy(update={"hard_max_output_tokens": 512})
        }
    )
    assert canonical_routing_invocation_bytes(
        first.invocation
    ) != canonical_routing_invocation_bytes(changed_profile)
    changed_provider_contract = first.invocation.model_copy(
        update={
            "profile": first.invocation.profile.model_copy(
                update={"provider_contract_version": "recorded-provider-contract.v2"}
            )
        }
    )
    assert canonical_routing_invocation_bytes(
        first.invocation
    ) != canonical_routing_invocation_bytes(changed_provider_contract)


def test_envelope_schema_has_no_raw_history_or_mcp_payload_fields() -> None:
    envelope = _assembly().envelope

    assert set(envelope.model_dump()) == {
        "schema_version",
        "context_policy_version",
        "current_input",
        "has_active_trip",
        "has_origin",
        "has_destination",
        "has_previous_destination",
        "has_facility_target",
        "can_use_stairs_known",
        "can_use_escalator_known",
        "available_capabilities",
    }
    with pytest.raises(ValidationError):
        ModelContextEnvelope.model_validate(
            {
                **envelope.model_dump(),
                "conversation_history": ("민감한 과거 질문",),
                "raw_mcp_payload": {"station": "서울역"},
            }
        )


def test_ephemeral_prompt_material_is_redacted_from_repr_and_error_strings() -> None:
    sentinel = "PRIVATE-STATION-QUESTION-SENTINEL"
    assembly = _assembly(current_input=sentinel)

    for rendered in (
        repr(assembly),
        str(assembly),
        repr(assembly.envelope),
        str(assembly.envelope),
        repr(assembly.invocation),
        str(assembly.invocation),
    ):
        assert sentinel not in rendered

    with pytest.raises(ValidationError) as captured:
        _assembly(current_input=f"  {sentinel}  ")
    assert sentinel not in str(captured.value)


@pytest.mark.asyncio
async def test_reservation_material_couples_count_output_cap_and_rebuilt_identity() -> None:
    sentinel = "PRIVATE-RESERVATION-SENTINEL"
    invocation = _assembly(current_input=sentinel).invocation
    material = await prepare_routing_reservation_material(
        invocation,
        counter=FallbackTokenCounter(),
        logical_call_index=2,
        usage_revision=1,
    )
    signer = ModelReservationFingerprintSigner(b"fake-routing-reservation-root-key")
    reservation = open_model_reservation(
        material.identity,
        material.canonical_invocation,
        signer=signer,
    )

    assert material.identity.estimated_input_tokens == material.token_count.input_tokens
    assert material.identity.counter_version == material.token_count.counter_version
    assert material.identity.provider_contract_version == (
        invocation.profile.provider_contract_version
    )
    assert material.identity.output_reserve_tokens == invocation.max_output_tokens
    assert material.identity.logical_call_index == 2
    assert material.identity.usage_revision == 1
    assert sentinel not in repr(material)
    assert sentinel not in str(material)
    assert verify_model_reservation(
        reservation,
        material.identity,
        material.canonical_invocation,
        signer=signer,
    )
