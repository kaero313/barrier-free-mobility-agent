from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol, cast

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
)
from openai.types.responses import (
    EasyInputMessageParam,
    Response,
    ResponseTextConfigParam,
)
from openai.types.shared_params.reasoning import Reasoning
from pydantic import BaseModel, SecretStr, ValidationError

from barrier_free_mobility_agent.application.model_boundary import (
    ModelFailure,
    ModelFailureKind,
    ModelInvocation,
    ModelInvocationResult,
    ModelMessageRole,
    ModelResult,
    StructuredOutputMode,
    TokenCount,
    TokenCountKind,
)
from barrier_free_mobility_agent.domain.model_routing import StructuredIntentDecision
from barrier_free_mobility_agent.domain.usage import ModelUsage, normalize_model_usage
from barrier_free_mobility_agent.integrations.models.contracts import (
    OPENAI_GPT_5_4_NANO_CONTRACT,
    HostedModelContract,
)

_DEFAULT_TIMEOUT_SECONDS = 20.0
_MAX_TIMEOUT_SECONDS = 120.0
_SCHEMA_NAME = "structured_intent_decision_v1"


class HostedModelContractError(ValueError):
    """A payload-free error raised before any hosted request is attempted."""

    def __init__(self, safe_error_code: str = "OPENAI_CONTRACT_REJECTED") -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class HostedTokenCountError(RuntimeError):
    """A bounded count failure that never retains an SDK exception or request body."""

    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


@dataclass(frozen=True, slots=True, repr=False)
class _WireMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True, repr=False)
class OpenAIResponsesWire:
    """Compiled immutable request material shared by count and inference."""

    model_snapshot: str
    instructions: str
    messages: tuple[_WireMessage, ...]
    response_schema_json: bytes
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"]
    text_verbosity: Literal["low", "medium", "high"]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    __str__ = __repr__

    def sdk_input(self) -> list[EasyInputMessageParam]:
        return [
            EasyInputMessageParam(content=message.content, role=message.role, type="message")
            for message in self.messages
        ]

    def sdk_reasoning(self) -> Reasoning:
        return Reasoning(effort=self.reasoning_effort)

    def sdk_text(self) -> ResponseTextConfigParam:
        schema = cast(dict[str, object], json.loads(self.response_schema_json))
        return ResponseTextConfigParam(
            format={
                "type": "json_schema",
                "name": _SCHEMA_NAME,
                "schema": schema,
                "strict": True,
            },
            verbosity=self.text_verbosity,
        )

    def canonical_shared_bytes(self) -> bytes:
        return json.dumps(
            {
                "input": [
                    {"content": message.content, "role": message.role, "type": "message"}
                    for message in self.messages
                ],
                "instructions": self.instructions,
                "model": self.model_snapshot,
                "parallel_tool_calls": False,
                "reasoning": {"effort": self.reasoning_effort},
                "text": self.sdk_text(),
                "tool_choice": "none",
                "tools": [],
                "truncation": "disabled",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class OpenAIInputTokenCount:
    object: str
    input_tokens: int


def compile_openai_responses_wire[ResultT: BaseModel](
    invocation: ModelInvocation[ResultT],
    *,
    contract: HostedModelContract = OPENAI_GPT_5_4_NANO_CONTRACT,
) -> OpenAIResponsesWire:
    """Validate the pinned contract and compile the only allowed provider wire."""

    if invocation.profile != contract.model_profile():
        raise HostedModelContractError("OPENAI_PROFILE_MISMATCH")
    if invocation.structured_output_mode != StructuredOutputMode.JSON_SCHEMA:
        raise HostedModelContractError("OPENAI_OUTPUT_MODE_UNSUPPORTED")
    if invocation.response_model is not StructuredIntentDecision:
        raise HostedModelContractError("OPENAI_RESPONSE_SCHEMA_UNSUPPORTED")
    if invocation.provider_options:
        # Secrets, endpoint overrides, retries, tools, and sampling options are all denied.
        raise HostedModelContractError("OPENAI_PROVIDER_OPTIONS_UNSUPPORTED")
    if invocation.max_output_tokens < contract.minimum_application_output_tokens:
        raise HostedModelContractError("OPENAI_OUTPUT_RESERVE_TOO_SMALL")
    if invocation.max_output_tokens > contract.hard_max_output_tokens:
        raise HostedModelContractError("OPENAI_OUTPUT_RESERVE_TOO_LARGE")

    schema_bytes = json.dumps(
        invocation.response_model.model_json_schema(mode="validation"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    messages = tuple(
        _WireMessage(
            role=("user" if message.role == ModelMessageRole.USER else "assistant"),
            content=message.content,
        )
        for message in invocation.messages
    )
    return OpenAIResponsesWire(
        model_snapshot=contract.provider_model_snapshot,
        instructions=invocation.system_policy,
        messages=messages,
        response_schema_json=schema_bytes,
        reasoning_effort=contract.reasoning_effort,
        text_verbosity=contract.text_verbosity,
    )


class OpenAIResponsesTransport(Protocol):
    async def count_input(self, wire: OpenAIResponsesWire) -> OpenAIInputTokenCount: ...

    async def create_response(
        self,
        wire: OpenAIResponsesWire,
        *,
        max_output_tokens: int,
    ) -> Response: ...

    async def aclose(self) -> None: ...


class OpenAIResponsesSDKTransport:
    """Official SDK transport with a fixed origin, bounded timeout, and zero retries."""

    __slots__ = ("_client", "_http_client", "_timeout")

    def __init__(
        self,
        *,
        api_key: SecretStr,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        secret = api_key.get_secret_value()
        if not secret:
            raise ValueError("OPENAI_API_KEY_REQUIRED")
        if not (0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS):
            raise ValueError("OPENAI_TIMEOUT_INVALID")
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0))
        self._http_client = http_client or httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            timeout=self._timeout,
        )
        self._client = AsyncOpenAI(
            api_key=secret,
            base_url="https://api.openai.com/v1",
            http_client=self._http_client,
            max_retries=0,
            timeout=self._timeout,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    async def count_input(self, wire: OpenAIResponsesWire) -> OpenAIInputTokenCount:
        response = await self._client.responses.input_tokens.count(
            input=wire.sdk_input(),
            instructions=wire.instructions,
            model=wire.model_snapshot,
            parallel_tool_calls=False,
            reasoning=wire.sdk_reasoning(),
            text=wire.sdk_text(),
            tool_choice="none",
            tools=[],
            truncation="disabled",
            timeout=self._timeout,
        )
        return OpenAIInputTokenCount(
            object=response.object,
            input_tokens=response.input_tokens,
        )

    async def create_response(
        self,
        wire: OpenAIResponsesWire,
        *,
        max_output_tokens: int,
    ) -> Response:
        response = await self._client.responses.create(  # type: ignore[call-overload]
            background=False,
            input=wire.sdk_input(),
            instructions=wire.instructions,
            max_output_tokens=max_output_tokens,
            model=wire.model_snapshot,
            parallel_tool_calls=False,
            reasoning=wire.sdk_reasoning(),
            store=False,
            stream=False,
            text=wire.sdk_text(),
            tool_choice="none",
            tools=[],
            truncation="disabled",
            timeout=self._timeout,
        )
        return cast(Response, response)

    async def aclose(self) -> None:
        await self._client.close()


class OpenAIResponsesStructuredModel:
    """Evaluation-ready adapter; production graph activation is intentionally separate."""

    def __init__(
        self,
        transport: OpenAIResponsesTransport,
        *,
        contract: HostedModelContract = OPENAI_GPT_5_4_NANO_CONTRACT,
    ) -> None:
        self._transport = transport
        self._contract = contract

    @classmethod
    def from_api_key(
        cls,
        api_key: SecretStr,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        contract: HostedModelContract = OPENAI_GPT_5_4_NANO_CONTRACT,
    ) -> OpenAIResponsesStructuredModel:
        return cls(
            OpenAIResponsesSDKTransport(
                api_key=api_key,
                timeout_seconds=timeout_seconds,
            ),
            contract=contract,
        )

    @property
    def version(self) -> str:
        return self._contract.counter_version

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    async def count_input[ResultT: BaseModel](
        self,
        invocation: ModelInvocation[ResultT],
    ) -> TokenCount:
        wire = compile_openai_responses_wire(invocation, contract=self._contract)
        count: OpenAIInputTokenCount | None = None
        error_code: str | None = None
        try:
            count = await self._transport.count_input(wire)
        except Exception as error:
            error_code = _count_error_code(error)
        if error_code is not None:
            # Raising outside the SDK exception handler prevents request/body retention
            # through ``__context__`` as well as through a visible exception chain.
            raise HostedTokenCountError(error_code)
        if (
            count is None
            or count.object != "response.input_tokens"
            or type(count.input_tokens) is not int
            or count.input_tokens < 0
        ):
            raise HostedTokenCountError("OPENAI_COUNT_INVALID")
        return TokenCount(
            input_tokens=count.input_tokens,
            kind=TokenCountKind.EXACT,
            counter_version=self.version,
            canonical_bytes=len(wire.canonical_shared_bytes()),
            logical_blocks=len(wire.messages) + 2,
        )

    async def invoke_structured[ResultT: BaseModel](
        self,
        invocation: ModelInvocation[ResultT],
        *,
        preflight: TokenCount | None = None,
    ) -> ModelInvocationResult[ResultT]:
        try:
            wire = compile_openai_responses_wire(invocation, contract=self._contract)
        except HostedModelContractError as error:
            return ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                safe_error_code=error.safe_error_code,
            )
        if (
            preflight is None
            or preflight.kind != TokenCountKind.EXACT
            or preflight.counter_version != self.version
            or preflight.canonical_bytes != len(wire.canonical_shared_bytes())
        ):
            return ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                safe_error_code="OPENAI_PREFLIGHT_MISMATCH",
            )

        try:
            response = await self._transport.create_response(
                wire,
                max_output_tokens=invocation.max_output_tokens,
            )
        except Exception as error:
            kind, code = _invocation_error(error)
            return ModelFailure(kind=kind, safe_error_code=code)

        usage = _normalized_response_usage(
            response,
            estimated_input_tokens=preflight.input_tokens,
            estimated_output_tokens=invocation.max_output_tokens,
            estimator_version=preflight.counter_version,
        )
        if response.model != self._contract.provider_model_snapshot:
            return ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                usage=usage,
                safe_error_code="OPENAI_MODEL_IDENTITY_MISMATCH",
            )
        if (
            response.max_output_tokens != invocation.max_output_tokens
            or usage.output_tokens > invocation.max_output_tokens
        ):
            return ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                usage=usage,
                safe_error_code="OPENAI_OUTPUT_CAP_MISMATCH",
            )
        if response.status != "completed":
            return ModelFailure(
                kind=(
                    ModelFailureKind.INVALID_STRUCTURED_OUTPUT
                    if response.status == "incomplete"
                    else ModelFailureKind.PROVIDER_ERROR
                ),
                usage=usage,
                safe_error_code=(
                    "OPENAI_RESPONSE_INCOMPLETE"
                    if response.status == "incomplete"
                    else "OPENAI_RESPONSE_NOT_COMPLETED"
                ),
            )
        if _contains_refusal(response):
            return ModelFailure(
                kind=ModelFailureKind.INVALID_STRUCTURED_OUTPUT,
                usage=usage,
                safe_error_code="OPENAI_RESPONSE_REFUSED",
            )
        output_text = _single_output_text(response)
        if output_text is None:
            return ModelFailure(
                kind=ModelFailureKind.INVALID_STRUCTURED_OUTPUT,
                usage=usage,
                safe_error_code="OPENAI_OUTPUT_CARDINALITY_INVALID",
            )
        try:
            value = invocation.response_model.model_validate_json(
                output_text,
                strict=True,
            )
        except (ValidationError, ValueError, TypeError):
            return ModelFailure(
                kind=ModelFailureKind.INVALID_STRUCTURED_OUTPUT,
                usage=usage,
                safe_error_code="OPENAI_STRUCTURED_OUTPUT_INVALID",
            )
        return ModelResult(
            value=value,
            usage=usage,
            provider_alias=invocation.profile.provider_alias,
            model_alias=invocation.profile.model_alias,
            provider_contract_version=invocation.profile.provider_contract_version,
            prompt_version=invocation.prompt_version,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


def _normalized_response_usage(
    response: Response,
    *,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    estimator_version: str,
) -> ModelUsage:
    usage = response.usage
    input_details = usage.input_tokens_details if usage is not None else None
    output_details = usage.output_tokens_details if usage is not None else None
    return normalize_model_usage(
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        estimator_version=estimator_version,
        reported_input_tokens=(usage.input_tokens if usage is not None else None),
        reported_output_tokens=(usage.output_tokens if usage is not None else None),
        reported_total_tokens=(usage.total_tokens if usage is not None else None),
        reported_cached_input_tokens=(
            getattr(input_details, "cached_tokens", None) if input_details is not None else None
        ),
        reported_reasoning_output_tokens=(
            getattr(output_details, "reasoning_tokens", None)
            if output_details is not None
            else None
        ),
    )


def _contains_refusal(response: Response) -> bool:
    return any(
        getattr(content, "type", None) == "refusal"
        for output in response.output
        if getattr(output, "type", None) == "message"
        for content in getattr(output, "content", ())
    )


def _single_output_text(response: Response) -> str | None:
    texts = [
        content.text
        for output in response.output
        if getattr(output, "type", None) == "message"
        for content in getattr(output, "content", ())
        if getattr(content, "type", None) == "output_text"
    ]
    return texts[0] if len(texts) == 1 and texts[0] else None


def _count_error_code(error: Exception) -> str:
    if isinstance(error, APITimeoutError):
        return "OPENAI_COUNT_TIMEOUT"
    if isinstance(error, APIConnectionError):
        return "OPENAI_COUNT_TRANSPORT"
    return "OPENAI_COUNT_PROVIDER_ERROR"


def _invocation_error(error: Exception) -> tuple[ModelFailureKind, str]:
    if isinstance(error, APITimeoutError):
        return ModelFailureKind.TIMEOUT, "OPENAI_REQUEST_TIMEOUT"
    if isinstance(error, APIConnectionError):
        return ModelFailureKind.TRANSPORT, "OPENAI_REQUEST_TRANSPORT"
    if isinstance(error, (APIStatusError, OpenAIError)):
        return ModelFailureKind.PROVIDER_ERROR, "OPENAI_PROVIDER_ERROR"
    return ModelFailureKind.PROVIDER_ERROR, "OPENAI_PROVIDER_MALFORMED"
