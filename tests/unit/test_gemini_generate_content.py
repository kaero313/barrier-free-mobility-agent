from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from barrier_free_mobility_agent.application.model_boundary import (
    ModelFailure,
    ModelFailureKind,
    ModelInvocation,
    ModelMessage,
    ModelMessageRole,
    ModelPurpose,
    ModelResult,
    ProviderRequestOption,
    TokenCount,
    TokenCountKind,
)
from barrier_free_mobility_agent.domain.model_routing import StructuredIntentDecision
from barrier_free_mobility_agent.domain.routing import AgentIntent
from barrier_free_mobility_agent.domain.usage import UsageSource
from barrier_free_mobility_agent.integrations.models.contracts import (
    GEMINI_3_1_FLASH_LITE_CONTRACT,
    OPENAI_GPT_5_4_NANO_CONTRACT,
)
from barrier_free_mobility_agent.integrations.models.gemini_generate_content import (
    GeminiContractError,
    GeminiHttpStatusError,
    GeminiHttpTransport,
    GeminiStructuredModel,
    GeminiTokenCountError,
    compile_gemini_wire,
    to_gemini_schema,
)

_API_KEY = "test-gemini-adapter-secret"
_SYSTEM_POLICY = "비공개 시스템 정책 prompt-secret-marker"
_USER_INPUT = "서울역에서 시청역까지 휠체어로 갈 수 있나요? user-secret-marker"
_MODEL = "gemini-3.1-flash-lite"
_COUNT_PATH = f"/v1beta/models/{_MODEL}:countTokens"
_GENERATE_PATH = f"/v1beta/models/{_MODEL}:generateContent"
_SUCCESS_OUTPUT = json.dumps(
    {"schema_version": "1", "intent": "trip_accessibility", "confidence": 0.96},
    separators=(",", ":"),
)


def _invocation(**updates: object) -> ModelInvocation[StructuredIntentDecision]:
    values: dict[str, object] = {
        "purpose": ModelPurpose.AMBIGUOUS_ROUTING,
        "prompt_version": "ambiguous-routing.v1",
        "context_policy_version": "minimal-context.v1",
        "profile": GEMINI_3_1_FLASH_LITE_CONTRACT.model_profile(),
        "system_policy": _SYSTEM_POLICY,
        "messages": (
            ModelMessage(role=ModelMessageRole.USER, content=_USER_INPUT),
            ModelMessage(role=ModelMessageRole.ASSISTANT, content="출발지를 확인했습니다."),
        ),
        "response_model": StructuredIntentDecision,
        "max_output_tokens": 512,
    }
    values.update(updates)
    return ModelInvocation[StructuredIntentDecision](**values)  # type: ignore[arg-type]


def _usage(
    *,
    prompt: int | None = 40,
    candidates: int | None = 30,
    thoughts: int | None = None,
    total: int | None = None,
    cached: int | None = None,
) -> dict[str, object]:
    usage: dict[str, object] = {}
    if prompt is not None:
        usage["promptTokenCount"] = prompt
    if candidates is not None:
        usage["candidatesTokenCount"] = candidates
    if thoughts is not None:
        usage["thoughtsTokenCount"] = thoughts
    if cached is not None:
        usage["cachedContentTokenCount"] = cached
    usage["totalTokenCount"] = (
        total if total is not None else (prompt or 0) + (candidates or 0) + (thoughts or 0)
    )
    return usage


def _payload(
    *,
    texts: tuple[str, ...] = (_SUCCESS_OUTPUT,),
    finish_reason: str = "STOP",
    model_version: str = _MODEL,
    usage: dict[str, object] | None = None,
    block_reason: str | None = None,
    candidate_count: int = 1,
    thought_text: str | None = None,
) -> dict[str, Any]:
    parts: list[dict[str, object]] = []
    if thought_text is not None:
        parts.append({"text": thought_text, "thought": True})
    parts.extend({"text": text} for text in texts)
    candidate = {"content": {"role": "model", "parts": parts}, "finishReason": finish_reason}
    body: dict[str, Any] = {
        "candidates": [candidate] * candidate_count,
        "modelVersion": model_version,
        "usageMetadata": _usage() if usage is None else usage,
    }
    if block_reason is not None:
        body["promptFeedback"] = {"blockReason": block_reason}
    return body


class _FakeTransport:
    def __init__(
        self,
        *,
        count: dict[str, object] | None = None,
        response: dict[str, Any] | None = None,
        count_error: Exception | None = None,
        generate_error: Exception | None = None,
    ) -> None:
        self.count_calls = 0
        self.generate_calls = 0
        self._count: dict[str, object] = {"totalTokens": 40} if count is None else count
        self._response = response
        self._count_error = count_error
        self._generate_error = generate_error

    async def count_tokens(self, wire: object) -> dict[str, object]:
        self.count_calls += 1
        if self._count_error is not None:
            raise self._count_error
        return self._count

    async def generate(self, wire: object) -> dict[str, Any]:
        self.generate_calls += 1
        if self._generate_error is not None:
            raise self._generate_error
        assert self._response is not None
        return self._response

    async def aclose(self) -> None:
        return None


def _preflight(**updates: object) -> TokenCount:
    wire = compile_gemini_wire(_invocation())
    values: dict[str, object] = {
        "input_tokens": 40,
        "kind": TokenCountKind.EXACT,
        "counter_version": GEMINI_3_1_FLASH_LITE_CONTRACT.counter_version,
        "canonical_bytes": len(wire.canonical_shared_bytes()),
        "logical_blocks": 4,
    }
    values.update(updates)
    return TokenCount(**values)  # type: ignore[arg-type]


def test_pinned_gemini_contract_has_stable_literal_canonical_sha() -> None:
    assert GEMINI_3_1_FLASH_LITE_CONTRACT.canonical_sha256() == (
        "d7e24f65c3b2d9d15c4a22fb93408742f3f5c7d59851ea9e3d7e5bc687a4c191"
    )
    # The provider publishes no dated build, so the pinned snapshot is a stable alias.
    assert GEMINI_3_1_FLASH_LITE_CONTRACT.provider_model_snapshot == _MODEL
    # Thinking cannot be disabled on this model, so `none` would be an untrue claim.
    assert GEMINI_3_1_FLASH_LITE_CONTRACT.reasoning_effort == "low"
    assert GEMINI_3_1_FLASH_LITE_CONTRACT.reasoning_tokens_in_output_cap is True


def test_schema_transform_keeps_the_closed_intent_set_and_drops_unsupported_keywords() -> None:
    raw = StructuredIntentDecision.model_json_schema(mode="validation")
    schema = to_gemini_schema(raw, raw.get("$defs", {}))
    serialized = json.dumps(schema)

    assert "$defs" not in serialized
    assert "$ref" not in serialized
    assert "additionalProperties" not in serialized
    assert "const" not in serialized

    properties = schema["properties"]
    # A literal becomes a single-value enum, and every enum keeps an explicit type
    # because the provider silently ignores an enum whose type is missing.
    assert properties["schema_version"] == {"enum": ["1"], "type": "string"}
    assert properties["intent"]["type"] == "string"
    assert set(properties["intent"]["enum"]) == {intent.value for intent in AgentIntent}
    assert properties["confidence"] == {"type": "number", "minimum": 0.0, "maximum": 1.0}
    assert set(schema["required"]) == {"schema_version", "intent", "confidence"}


@pytest.mark.asyncio
async def test_real_transport_uses_fixed_paths_header_auth_and_one_shared_wire() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 40})
        return httpx.Response(200, json=_payload())

    transport = GeminiHttpTransport(
        api_key=SecretStr(_API_KEY),
        transport=httpx.MockTransport(handler),
    )
    model = GeminiStructuredModel(transport)
    invocation = _invocation()
    try:
        count = await model.count_input(invocation)
        result = await model.invoke_structured(invocation, preflight=count)
    finally:
        await model.aclose()

    assert isinstance(result, ModelResult)
    assert [request.url.path for request in seen] == [_COUNT_PATH, _GENERATE_PATH]
    for request in seen:
        assert request.url.scheme == "https"
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == _API_KEY
        # The key must never travel in the URL, where proxies and logs would keep it.
        assert "key=" not in str(request.url)

    counted = json.loads(seen[0].content)["generateContentRequest"]
    generated = json.loads(seen[1].content)
    assert counted["model"] == f"models/{_MODEL}"
    # Counting anything other than the exact charged request would make the preflight
    # an estimate wearing an exact label.
    assert {key: value for key, value in counted.items() if key != "model"} == generated


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["profile", "provider_options", "reserve"])
async def test_contract_rejections_make_zero_transport_calls(rejection: str) -> None:
    rejections: dict[str, dict[str, object]] = {
        "profile": {"profile": OPENAI_GPT_5_4_NANO_CONTRACT.model_profile()},
        "provider_options": {
            "provider_options": (ProviderRequestOption(name="temperature", value="0"),)
        },
        "reserve": {"max_output_tokens": 8},
    }
    updates = rejections[rejection]
    transport = _FakeTransport()
    model = GeminiStructuredModel(transport)
    invocation = _invocation(**updates)

    result = await model.invoke_structured(invocation, preflight=_preflight())

    assert isinstance(result, ModelFailure)
    assert result.kind is ModelFailureKind.PROVIDER_ERROR
    assert transport.count_calls == 0
    assert transport.generate_calls == 0
    with pytest.raises(GeminiContractError):
        compile_gemini_wire(invocation)


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight", [None, "estimated", "stale_counter", "stale_bytes"])
async def test_preflight_rejections_make_zero_generate_calls(preflight: str | None) -> None:
    supplied = {
        None: None,
        "estimated": _preflight(kind=TokenCountKind.ESTIMATED),
        "stale_counter": _preflight(counter_version="fallback-v1"),
        "stale_bytes": _preflight(canonical_bytes=1),
    }[preflight]
    transport = _FakeTransport(response=_payload())
    model = GeminiStructuredModel(transport)

    result = await model.invoke_structured(_invocation(), preflight=supplied)

    assert isinstance(result, ModelFailure)
    assert result.safe_error_code == "GEMINI_PREFLIGHT_MISMATCH"
    assert transport.generate_calls == 0


@pytest.mark.asyncio
async def test_success_returns_strict_value_identity_and_reported_usage() -> None:
    model = GeminiStructuredModel(_FakeTransport(response=_payload()))
    invocation = _invocation()

    result = await model.invoke_structured(invocation, preflight=_preflight())

    assert isinstance(result, ModelResult)
    assert result.value == StructuredIntentDecision(
        schema_version="1",
        intent=AgentIntent.TRIP_ACCESSIBILITY,
        confidence=0.96,
    )
    assert result.provider_alias == "google-generative-language"
    assert result.model_alias == _MODEL
    assert result.provider_contract_version == GEMINI_3_1_FLASH_LITE_CONTRACT.contract_version
    assert result.prompt_version == invocation.prompt_version
    assert result.usage.source is UsageSource.REPORTED
    assert result.usage.input_tokens == 40
    assert result.usage.output_tokens == 30


@pytest.mark.asyncio
async def test_thought_tokens_are_billed_as_output_and_bounded_by_the_reserve() -> None:
    model = GeminiStructuredModel(
        _FakeTransport(response=_payload(usage=_usage(candidates=10, thoughts=25)))
    )

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelResult)
    assert result.usage.output_tokens == 35
    assert result.usage.reasoning_output_tokens == 25


@pytest.mark.asyncio
async def test_thoughts_overrunning_the_reserve_are_rejected_with_usage_preserved() -> None:
    # The provider caps thoughts and visible output together today. If that ever stops
    # being true the run must fail closed instead of quietly billing the overrun.
    model = GeminiStructuredModel(
        _FakeTransport(response=_payload(usage=_usage(candidates=20, thoughts=600)))
    )

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelFailure)
    assert result.safe_error_code == "GEMINI_OUTPUT_CAP_MISMATCH"
    assert result.usage is not None
    assert result.usage.output_tokens == 620


@pytest.mark.asyncio
async def test_thought_parts_are_never_read_as_structured_output() -> None:
    model = GeminiStructuredModel(_FakeTransport(response=_payload(thought_text="내부 추론 흔적")))

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelResult)
    assert result.value.intent is AgentIntent.TRIP_ACCESSIBILITY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_output",
    [
        '{"schema_version":"1","intent":"trip_accessibility"}',
        '{"schema_version":"1","intent":"unsupported","confidence":0.5}',
        '{"schema_version":"1","intent":"trip_accessibility","confidence":"0.9"}',
        '{"schema_version":"1","intent":"trip_accessibility","confidence":1.5}',
        '{"schema_version":"1","intent":"trip_accessibility","confidence":0.9,"extra":1}',
        "not json at all",
    ],
)
async def test_invalid_or_non_strict_structured_output_is_rejected(invalid_output: str) -> None:
    model = GeminiStructuredModel(_FakeTransport(response=_payload(texts=(invalid_output,))))

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelFailure)
    assert result.kind is ModelFailureKind.INVALID_STRUCTURED_OUTPUT
    assert result.safe_error_code == "GEMINI_STRUCTURED_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_blocked_prompt_is_never_treated_as_structured_output() -> None:
    model = GeminiStructuredModel(
        _FakeTransport(response=_payload(texts=(), block_reason="SAFETY"))
    )

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelFailure)
    assert result.kind is ModelFailureKind.INVALID_STRUCTURED_OUTPUT
    assert result.safe_error_code == "GEMINI_PROMPT_BLOCKED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "expected_kind", "expected_code"),
    [
        ("MAX_TOKENS", ModelFailureKind.INVALID_STRUCTURED_OUTPUT, "GEMINI_RESPONSE_INCOMPLETE"),
        ("SAFETY", ModelFailureKind.INVALID_STRUCTURED_OUTPUT, "GEMINI_RESPONSE_BLOCKED"),
        ("RECITATION", ModelFailureKind.INVALID_STRUCTURED_OUTPUT, "GEMINI_RESPONSE_BLOCKED"),
        ("OTHER", ModelFailureKind.PROVIDER_ERROR, "GEMINI_RESPONSE_NOT_COMPLETED"),
    ],
)
async def test_non_stop_finish_reason_is_fail_closed(
    finish_reason: str,
    expected_kind: ModelFailureKind,
    expected_code: str,
) -> None:
    model = GeminiStructuredModel(_FakeTransport(response=_payload(finish_reason=finish_reason)))

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelFailure)
    assert result.kind is expected_kind
    assert result.safe_error_code == expected_code


@pytest.mark.asyncio
async def test_model_identity_drift_is_rejected_with_usage_preserved() -> None:
    model = GeminiStructuredModel(
        _FakeTransport(response=_payload(model_version="gemini-3.1-flash-lite-preview"))
    )

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelFailure)
    assert result.safe_error_code == "GEMINI_MODEL_IDENTITY_MISMATCH"
    assert result.usage is not None


@pytest.mark.asyncio
async def test_multiple_text_parts_are_rejected_instead_of_concatenated() -> None:
    model = GeminiStructuredModel(
        _FakeTransport(response=_payload(texts=(_SUCCESS_OUTPUT, _SUCCESS_OUTPUT)))
    )

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelFailure)
    assert result.safe_error_code == "GEMINI_OUTPUT_CARDINALITY_INVALID"


@pytest.mark.asyncio
async def test_multiple_candidates_are_rejected() -> None:
    model = GeminiStructuredModel(_FakeTransport(response=_payload(candidate_count=2)))

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelFailure)
    assert result.safe_error_code == "GEMINI_CANDIDATE_CARDINALITY_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_shape", ["missing", "partial"])
async def test_missing_or_partial_usage_is_conservatively_normalized(usage_shape: str) -> None:
    payload = _payload()
    if usage_shape == "missing":
        payload.pop("usageMetadata")
    else:
        payload["usageMetadata"] = {"promptTokenCount": 40}
    model = GeminiStructuredModel(_FakeTransport(response=payload))

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelResult)
    assert result.usage.source is not UsageSource.REPORTED
    assert result.usage.estimator_version == GEMINI_3_1_FLASH_LITE_CONTRACT.counter_version


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_kind", "expected_code"),
    [
        (httpx.ConnectTimeout("slow"), ModelFailureKind.TIMEOUT, "GEMINI_REQUEST_TIMEOUT"),
        (httpx.ConnectError("down"), ModelFailureKind.TRANSPORT, "GEMINI_REQUEST_TRANSPORT"),
        (GeminiHttpStatusError(429), ModelFailureKind.PROVIDER_ERROR, "GEMINI_PROVIDER_ERROR"),
        (ValueError("broken"), ModelFailureKind.PROVIDER_ERROR, "GEMINI_PROVIDER_MALFORMED"),
    ],
)
async def test_generate_failures_are_classified_without_payloads(
    error: Exception,
    expected_kind: ModelFailureKind,
    expected_code: str,
) -> None:
    model = GeminiStructuredModel(_FakeTransport(generate_error=error))

    result = await model.invoke_structured(_invocation(), preflight=_preflight())

    assert isinstance(result, ModelFailure)
    assert result.kind is expected_kind
    assert result.safe_error_code == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (httpx.ConnectTimeout("slow"), "GEMINI_COUNT_TIMEOUT"),
        (httpx.ConnectError("down"), "GEMINI_COUNT_TRANSPORT"),
        (GeminiHttpStatusError(500), "GEMINI_COUNT_PROVIDER_ERROR"),
    ],
)
async def test_count_failures_never_retain_the_provider_exception(
    error: Exception,
    expected_code: str,
) -> None:
    model = GeminiStructuredModel(_FakeTransport(count_error=error))

    with pytest.raises(GeminiTokenCountError) as raised:
        await model.count_input(_invocation())

    assert raised.value.safe_error_code == expected_code
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"totalTokens": -1}, {"totalTokens": "40"}])
async def test_malformed_counts_are_rejected_before_admission(payload: dict[str, object]) -> None:
    model = GeminiStructuredModel(_FakeTransport(count=payload))

    with pytest.raises(GeminiTokenCountError) as raised:
        await model.count_input(_invocation())

    assert raised.value.safe_error_code == "GEMINI_COUNT_INVALID"


def test_wire_and_adapter_never_repr_their_payloads() -> None:
    wire = compile_gemini_wire(_invocation())
    model = GeminiStructuredModel(_FakeTransport())
    transport = GeminiHttpTransport(api_key=SecretStr(_API_KEY))

    for rendered in (repr(wire), str(wire), repr(model), repr(transport)):
        assert "<redacted>" in rendered
        assert _SYSTEM_POLICY not in rendered
        assert _USER_INPUT not in rendered
        assert _API_KEY not in rendered
