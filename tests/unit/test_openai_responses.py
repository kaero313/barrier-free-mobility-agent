from __future__ import annotations

import json
from typing import Any, Literal, cast

import httpx
import pytest
from openai.types.responses import Response, ResponseUsage
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
    OPENAI_GPT_5_4_NANO_CONTRACT,
)
from barrier_free_mobility_agent.integrations.models.openai_responses import (
    HostedModelContractError,
    HostedTokenCountError,
    OpenAIInputTokenCount,
    OpenAIResponsesSDKTransport,
    OpenAIResponsesStructuredModel,
    OpenAIResponsesWire,
    compile_openai_responses_wire,
)

_API_KEY = "sk-test-hosted-adapter-secret"
_SYSTEM_POLICY = "비공개 시스템 정책 prompt-secret-marker"
_USER_INPUT = "서울역에서 시청역까지 휠체어로 갈 수 있나요? user-secret-marker"
_MODEL_SNAPSHOT = "gpt-5.4-nano-2026-03-17"
_COUNT_PATH = "/v1/responses/input_tokens"
_RESPONSE_PATH = "/v1/responses"
_SUCCESS_OUTPUT = json.dumps(
    {
        "schema_version": "1",
        "intent": "trip_accessibility",
        "confidence": 0.96,
    },
    separators=(",", ":"),
)


def _invocation(**updates: object) -> ModelInvocation[StructuredIntentDecision]:
    values: dict[str, object] = {
        "purpose": ModelPurpose.AMBIGUOUS_ROUTING,
        "prompt_version": "ambiguous-routing.v1",
        "context_policy_version": "minimal-context.v1",
        "profile": OPENAI_GPT_5_4_NANO_CONTRACT.model_profile(),
        "system_policy": _SYSTEM_POLICY,
        "messages": (
            ModelMessage(role=ModelMessageRole.USER, content=_USER_INPUT),
            ModelMessage(
                role=ModelMessageRole.ASSISTANT,
                content="출발지와 도착지를 확인했습니다.",
            ),
        ),
        "response_model": StructuredIntentDecision,
        "max_output_tokens": 512,
    }
    values.update(updates)
    return ModelInvocation[StructuredIntentDecision](**values)  # type: ignore[arg-type]


def _response_payload(
    *,
    output_texts: tuple[str, ...] = (_SUCCESS_OUTPUT,),
    status: str = "completed",
    model: str = _MODEL_SNAPSHOT,
    max_output_tokens: int | None = 512,
    refusal: str | None = None,
    usage: dict[str, object] | None | Literal["default"] = "default",
) -> dict[str, object]:
    if refusal is not None:
        content: list[dict[str, object]] = [{"type": "refusal", "refusal": refusal}]
    else:
        content = [
            {"type": "output_text", "text": text, "annotations": []} for text in output_texts
        ]
    output: list[dict[str, object]] = []
    if content:
        output.append(
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "incomplete" if status == "incomplete" else "completed",
                "content": content,
            }
        )
    if usage == "default":
        resolved_usage: dict[str, object] | None = {
            "input_tokens": 41,
            "input_tokens_details": {
                "cache_write_tokens": 0,
                "cached_tokens": 11,
            },
            "output_tokens": 9,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 50,
        }
    else:
        resolved_usage = usage
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1_783_932_000.0,
        "model": model,
        "max_output_tokens": max_output_tokens,
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "none",
        "tools": [],
        "status": status,
        "usage": resolved_usage,
    }


def _response(**updates: Any) -> Response:
    return Response.model_validate(_response_payload(**updates))


def _preflight(
    invocation: ModelInvocation[StructuredIntentDecision],
    *,
    input_tokens: int = 41,
    kind: TokenCountKind = TokenCountKind.EXACT,
    counter_version: str | None = None,
    canonical_bytes: int | None = None,
) -> TokenCount:
    wire = compile_openai_responses_wire(invocation)
    return TokenCount(
        input_tokens=input_tokens,
        kind=kind,
        counter_version=(
            counter_version
            if counter_version is not None
            else OPENAI_GPT_5_4_NANO_CONTRACT.counter_version
        ),
        canonical_bytes=(
            canonical_bytes if canonical_bytes is not None else len(wire.canonical_shared_bytes())
        ),
        logical_blocks=len(wire.messages) + 2,
    )


class _FakeTransport:
    def __init__(
        self,
        *,
        response: Response | None = None,
        count: OpenAIInputTokenCount | None = None,
    ) -> None:
        self.response = response or _response()
        self.count = count or OpenAIInputTokenCount(
            object="response.input_tokens",
            input_tokens=41,
        )
        self.count_calls: list[OpenAIResponsesWire] = []
        self.response_calls: list[tuple[OpenAIResponsesWire, int]] = []
        self.closed = False

    async def count_input(self, wire: OpenAIResponsesWire) -> OpenAIInputTokenCount:
        self.count_calls.append(wire)
        return self.count

    async def create_response(
        self,
        wire: OpenAIResponsesWire,
        *,
        max_output_tokens: int,
    ) -> Response:
        self.response_calls.append((wire, max_output_tokens))
        return self.response

    async def aclose(self) -> None:
        self.closed = True


def _assert_redacted(value: object) -> None:
    rendered = repr(value)
    assert _API_KEY not in rendered
    assert _SYSTEM_POLICY not in rendered
    assert _USER_INPUT not in rendered
    assert "prompt-secret-marker" not in rendered
    assert "user-secret-marker" not in rendered


def test_pinned_hosted_contract_has_stable_literal_canonical_sha() -> None:
    contract = OPENAI_GPT_5_4_NANO_CONTRACT

    assert contract.canonical_sha256() == (
        "c79c21ebef93ff77a61f2b63298609734602fd3a7d19b54b3ec8a3b2e1b57c94"
    )
    assert contract.provider_model_snapshot == _MODEL_SNAPSHOT
    assert contract.model_alias == _MODEL_SNAPSHOT
    assert contract.reasoning_effort == "none"
    assert contract.text_verbosity == "low"
    assert contract.reasoning_tokens_in_output_cap is True


@pytest.mark.asyncio
async def test_real_sdk_uses_fixed_https_paths_and_identical_count_invoke_wire() -> None:
    requests: list[tuple[str, dict[str, object], str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body, request.headers["authorization"]))
        assert request.url.scheme == "https"
        assert request.url.host == "api.openai.com"
        if request.url.path == _COUNT_PATH:
            return httpx.Response(
                200,
                json={"object": "response.input_tokens", "input_tokens": 41},
            )
        if request.url.path == _RESPONSE_PATH:
            return httpx.Response(200, json=_response_payload())
        raise AssertionError(f"unexpected SDK request path: {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = OpenAIResponsesSDKTransport(
        api_key=SecretStr(_API_KEY),
        http_client=client,
    )
    model = OpenAIResponsesStructuredModel(transport)
    invocation = _invocation()
    try:
        count = await model.count_input(invocation)
        result = await model.invoke_structured(invocation, preflight=count)
    finally:
        await model.aclose()

    assert isinstance(result, ModelResult)
    assert [path for path, _, _ in requests] == [_COUNT_PATH, _RESPONSE_PATH]
    assert all(header == f"Bearer {_API_KEY}" for _, _, header in requests)

    count_body = requests[0][1]
    invoke_body = requests[1][1]
    shared_keys = {
        "input",
        "instructions",
        "model",
        "parallel_tool_calls",
        "reasoning",
        "text",
        "tool_choice",
        "tools",
        "truncation",
    }
    assert set(count_body) == shared_keys
    assert set(invoke_body) == shared_keys | {
        "background",
        "max_output_tokens",
        "store",
        "stream",
    }
    assert {key: count_body[key] for key in shared_keys} == {
        key: invoke_body[key] for key in shared_keys
    }
    assert count_body["input"] == [
        {"content": _USER_INPUT, "role": "user", "type": "message"},
        {
            "content": "출발지와 도착지를 확인했습니다.",
            "role": "assistant",
            "type": "message",
        },
    ]
    assert count_body["instructions"] == _SYSTEM_POLICY
    assert count_body["model"] == _MODEL_SNAPSHOT
    assert count_body["parallel_tool_calls"] is False
    assert count_body["reasoning"] == {"effort": "none"}
    assert count_body["tool_choice"] == "none"
    assert count_body["tools"] == []
    assert count_body["truncation"] == "disabled"

    text = count_body["text"]
    assert isinstance(text, dict)
    assert text["verbosity"] == "low"
    response_format = text["format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    assert response_format["name"] == "structured_intent_decision_v1"
    assert response_format["strict"] is True
    assert response_format["schema"] == StructuredIntentDecision.model_json_schema(
        mode="validation"
    )
    assert response_format["schema"]["additionalProperties"] is False
    assert set(response_format["schema"]["required"]) == {
        "schema_version",
        "intent",
        "confidence",
    }

    assert invoke_body["max_output_tokens"] == invocation.max_output_tokens == 512
    assert invoke_body["background"] is False
    assert invoke_body["store"] is False
    assert invoke_body["stream"] is False
    assert count.kind == TokenCountKind.EXACT
    assert count.input_tokens == 41


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_contract", ["profile", "provider_options"])
async def test_contract_rejections_make_zero_transport_calls(invalid_contract: str) -> None:
    transport = _FakeTransport()
    model = OpenAIResponsesStructuredModel(transport)
    invocation = _invocation()
    if invalid_contract == "profile":
        profile = invocation.profile.model_copy(update={"provider_alias": "other-provider"})
        invocation = invocation.model_copy(update={"profile": profile})
        expected_code = "OPENAI_PROFILE_MISMATCH"
    else:
        invocation = invocation.model_copy(
            update={"provider_options": (ProviderRequestOption(name="temperature", value=0.0),)}
        )
        expected_code = "OPENAI_PROVIDER_OPTIONS_UNSUPPORTED"

    with pytest.raises(HostedModelContractError) as error:
        await model.count_input(invocation)
    result = await model.invoke_structured(invocation, preflight=None)

    assert error.value.safe_error_code == expected_code
    assert isinstance(result, ModelFailure)
    assert result.safe_error_code == expected_code
    assert transport.count_calls == []
    assert transport.response_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preflight",
    [
        None,
        "estimated",
        "wrong-version",
        "wrong-wire-size",
    ],
)
async def test_preflight_rejections_make_zero_response_calls(preflight: str | None) -> None:
    transport = _FakeTransport()
    model = OpenAIResponsesStructuredModel(transport)
    invocation = _invocation()
    if preflight == "estimated":
        candidate = _preflight(invocation, kind=TokenCountKind.ESTIMATED)
    elif preflight == "wrong-version":
        candidate = _preflight(invocation, counter_version="other-counter.v1")
    elif preflight == "wrong-wire-size":
        candidate = _preflight(invocation, canonical_bytes=1)
    else:
        candidate = None

    result = await model.invoke_structured(invocation, preflight=candidate)

    assert isinstance(result, ModelFailure)
    assert result.kind == ModelFailureKind.PROVIDER_ERROR
    assert result.safe_error_code == "OPENAI_PREFLIGHT_MISMATCH"
    assert transport.count_calls == []
    assert transport.response_calls == []


@pytest.mark.asyncio
async def test_success_returns_strict_value_identity_and_complete_reported_usage() -> None:
    transport = _FakeTransport(response=_response())
    model = OpenAIResponsesStructuredModel(transport)
    invocation = _invocation()

    result = await model.invoke_structured(invocation, preflight=_preflight(invocation))

    assert isinstance(result, ModelResult)
    assert result.value == StructuredIntentDecision(
        schema_version="1",
        intent=AgentIntent.TRIP_ACCESSIBILITY,
        confidence=0.96,
    )
    assert result.provider_alias == invocation.profile.provider_alias
    assert result.model_alias == invocation.profile.model_alias
    assert result.provider_contract_version == invocation.profile.provider_contract_version
    assert result.prompt_version == invocation.prompt_version
    assert result.usage.input_tokens == 41
    assert result.usage.output_tokens == 9
    assert result.usage.total_tokens == 50
    assert result.usage.cached_input_tokens == 11
    assert result.usage.reasoning_output_tokens == 2
    assert result.usage.source == UsageSource.REPORTED
    assert result.usage.estimator_version is None
    assert transport.response_calls[0][1] == invocation.max_output_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_output",
    [
        "{",
        ('{"schema_version":"1","intent":"trip_accessibility","confidence":0.9,"unexpected":true}'),
        '{"schema_version":1,"intent":"trip_accessibility","confidence":0.9}',
        ('{"schema_version":"1","intent":"trip_accessibility","confidence":"0.9"}'),
        '{"schema_version":"1","intent":"invented","confidence":0.9}',
    ],
    ids=["invalid-json", "extra", "coerced-literal", "coerced-number", "unknown-enum"],
)
async def test_invalid_or_non_strict_structured_output_is_rejected(
    invalid_output: str,
) -> None:
    invocation = _invocation()
    model = OpenAIResponsesStructuredModel(
        _FakeTransport(response=_response(output_texts=(invalid_output,)))
    )

    result = await model.invoke_structured(invocation, preflight=_preflight(invocation))

    assert isinstance(result, ModelFailure)
    assert result.kind == ModelFailureKind.INVALID_STRUCTURED_OUTPUT
    assert result.safe_error_code == "OPENAI_STRUCTURED_OUTPUT_INVALID"
    assert result.usage is not None


@pytest.mark.asyncio
async def test_refusal_is_never_treated_as_structured_output() -> None:
    invocation = _invocation()
    model = OpenAIResponsesStructuredModel(
        _FakeTransport(response=_response(output_texts=(), refusal="요청을 처리할 수 없습니다."))
    )

    result = await model.invoke_structured(invocation, preflight=_preflight(invocation))

    assert isinstance(result, ModelFailure)
    assert result.kind == ModelFailureKind.INVALID_STRUCTURED_OUTPUT
    assert result.safe_error_code == "OPENAI_RESPONSE_REFUSED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "kind", "code"),
    [
        (
            "incomplete",
            ModelFailureKind.INVALID_STRUCTURED_OUTPUT,
            "OPENAI_RESPONSE_INCOMPLETE",
        ),
        ("failed", ModelFailureKind.PROVIDER_ERROR, "OPENAI_RESPONSE_NOT_COMPLETED"),
    ],
)
async def test_non_completed_provider_status_is_fail_closed(
    status: str,
    kind: ModelFailureKind,
    code: str,
) -> None:
    invocation = _invocation()
    model = OpenAIResponsesStructuredModel(_FakeTransport(response=_response(status=status)))

    result = await model.invoke_structured(invocation, preflight=_preflight(invocation))

    assert isinstance(result, ModelFailure)
    assert result.kind == kind
    assert result.safe_error_code == code
    assert result.usage is not None


@pytest.mark.asyncio
async def test_provider_model_snapshot_drift_is_rejected_with_usage_preserved() -> None:
    invocation = _invocation()
    model = OpenAIResponsesStructuredModel(
        _FakeTransport(response=_response(model="gpt-5.4-nano-moving-alias"))
    )

    result = await model.invoke_structured(invocation, preflight=_preflight(invocation))

    assert isinstance(result, ModelFailure)
    assert result.kind == ModelFailureKind.PROVIDER_ERROR
    assert result.safe_error_code == "OPENAI_MODEL_IDENTITY_MISMATCH"
    assert result.usage is not None
    assert result.usage.source == UsageSource.REPORTED


@pytest.mark.asyncio
async def test_provider_output_cap_drift_is_rejected_with_usage_preserved() -> None:
    invocation = _invocation()
    model = OpenAIResponsesStructuredModel(
        _FakeTransport(response=_response(max_output_tokens=513))
    )

    result = await model.invoke_structured(invocation, preflight=_preflight(invocation))

    assert isinstance(result, ModelFailure)
    assert result.kind == ModelFailureKind.PROVIDER_ERROR
    assert result.safe_error_code == "OPENAI_OUTPUT_CAP_MISMATCH"
    assert result.usage is not None
    assert result.usage.source == UsageSource.REPORTED


@pytest.mark.asyncio
async def test_reported_output_token_overshoot_is_rejected_as_cap_drift() -> None:
    invocation = _invocation()
    response = _response(
        usage={
            "input_tokens": 41,
            "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
            "output_tokens": 513,
            "output_tokens_details": {"reasoning_tokens": 500},
            "total_tokens": 554,
        }
    )
    model = OpenAIResponsesStructuredModel(_FakeTransport(response=response))

    result = await model.invoke_structured(invocation, preflight=_preflight(invocation))

    assert isinstance(result, ModelFailure)
    assert result.safe_error_code == "OPENAI_OUTPUT_CAP_MISMATCH"
    assert result.usage is not None
    assert result.usage.output_tokens == 513


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_shape", ["missing", "partial"])
async def test_missing_or_partial_usage_is_conservatively_normalized(
    usage_shape: str,
) -> None:
    invocation = _invocation()
    response = _response(usage=None)
    if usage_shape == "partial":
        partial = ResponseUsage.model_construct(
            input_tokens=37,
            input_tokens_details=cast(Any, None),
            output_tokens=cast(Any, None),
            output_tokens_details=cast(Any, None),
            total_tokens=cast(Any, None),
        )
        response = response.model_copy(update={"usage": partial})
    model = OpenAIResponsesStructuredModel(_FakeTransport(response=response))

    result = await model.invoke_structured(
        invocation,
        preflight=_preflight(invocation, input_tokens=41),
    )

    assert isinstance(result, ModelResult)
    if usage_shape == "missing":
        assert result.usage.input_tokens == 41
        assert result.usage.source == UsageSource.ESTIMATED
    else:
        assert result.usage.input_tokens == 37
        assert result.usage.source == UsageSource.MIXED
    assert result.usage.output_tokens == invocation.max_output_tokens
    assert result.usage.total_tokens == result.usage.input_tokens + invocation.max_output_tokens
    assert result.usage.estimator_version == OPENAI_GPT_5_4_NANO_CONTRACT.counter_version


@pytest.mark.asyncio
async def test_multiple_output_text_blocks_are_rejected_instead_of_concatenated() -> None:
    invocation = _invocation()
    model = OpenAIResponsesStructuredModel(
        _FakeTransport(response=_response(output_texts=(_SUCCESS_OUTPUT, _SUCCESS_OUTPUT)))
    )

    result = await model.invoke_structured(invocation, preflight=_preflight(invocation))

    assert isinstance(result, ModelFailure)
    assert result.kind == ModelFailureKind.INVALID_STRUCTURED_OUTPUT
    assert result.safe_error_code == "OPENAI_OUTPUT_CARDINALITY_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["status", "timeout"])
async def test_real_sdk_count_has_zero_retries_and_payload_free_error(
    failure_mode: str,
) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.path == _COUNT_PATH
        if failure_mode == "timeout":
            raise httpx.ReadTimeout(
                f"{_SYSTEM_POLICY} {_USER_INPUT} {_API_KEY}",
                request=request,
            )
        return httpx.Response(
            500,
            request=request,
            json={"error": {"message": f"{_SYSTEM_POLICY} {_USER_INPUT} {_API_KEY}"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = OpenAIResponsesSDKTransport(
        api_key=SecretStr(_API_KEY),
        http_client=client,
    )
    model = OpenAIResponsesStructuredModel(transport)
    try:
        with pytest.raises(HostedTokenCountError) as error:
            await model.count_input(_invocation())
    finally:
        await model.aclose()

    assert attempts == 1
    assert error.value.safe_error_code == (
        "OPENAI_COUNT_TIMEOUT" if failure_mode == "timeout" else "OPENAI_COUNT_PROVIDER_ERROR"
    )
    _assert_redacted(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["status", "timeout"])
async def test_real_sdk_invoke_has_zero_retries_and_returns_only_safe_failure(
    failure_mode: str,
) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.path == _RESPONSE_PATH
        if failure_mode == "timeout":
            raise httpx.ReadTimeout(
                f"{_SYSTEM_POLICY} {_USER_INPUT} {_API_KEY}",
                request=request,
            )
        return httpx.Response(
            500,
            request=request,
            json={"error": {"message": f"{_SYSTEM_POLICY} {_USER_INPUT} {_API_KEY}"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = OpenAIResponsesSDKTransport(
        api_key=SecretStr(_API_KEY),
        http_client=client,
    )
    model = OpenAIResponsesStructuredModel(transport)
    invocation = _invocation()
    try:
        result = await model.invoke_structured(
            invocation,
            preflight=_preflight(invocation),
        )
    finally:
        await model.aclose()

    assert attempts == 1
    assert isinstance(result, ModelFailure)
    assert result.kind == (
        ModelFailureKind.TIMEOUT if failure_mode == "timeout" else ModelFailureKind.PROVIDER_ERROR
    )
    assert result.safe_error_code == (
        "OPENAI_REQUEST_TIMEOUT" if failure_mode == "timeout" else "OPENAI_PROVIDER_ERROR"
    )
    _assert_redacted(result)


@pytest.mark.asyncio
async def test_invalid_count_shape_is_rejected_and_close_is_delegated() -> None:
    transport = _FakeTransport(count=OpenAIInputTokenCount(object="wrong-object", input_tokens=-1))
    model = OpenAIResponsesStructuredModel(transport)

    with pytest.raises(HostedTokenCountError) as error:
        await model.count_input(_invocation())
    await model.aclose()

    assert error.value.safe_error_code == "OPENAI_COUNT_INVALID"
    assert len(transport.count_calls) == 1
    assert transport.response_calls == []
    assert transport.closed is True


@pytest.mark.asyncio
async def test_wire_invocation_transport_and_contract_errors_redact_inputs() -> None:
    invocation = _invocation()
    wire = compile_openai_responses_wire(invocation)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail(f"unexpected network request: {request.url}")
        )
    )
    transport = OpenAIResponsesSDKTransport(
        api_key=SecretStr(_API_KEY),
        http_client=client,
    )
    model = OpenAIResponsesStructuredModel(transport)
    try:
        _assert_redacted(invocation)
        _assert_redacted(wire)
        _assert_redacted(transport)
        _assert_redacted(model)
        error = HostedModelContractError("OPENAI_PROFILE_MISMATCH")
        _assert_redacted(error)
        assert str(error) == "OPENAI_PROFILE_MISMATCH"
    finally:
        await model.aclose()
