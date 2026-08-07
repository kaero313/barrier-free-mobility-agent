from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
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
    GEMINI_3_1_FLASH_LITE_CONTRACT,
    HostedModelContract,
)

_DEFAULT_TIMEOUT_SECONDS = 20.0
_MAX_TIMEOUT_SECONDS = 120.0
_BASE_URL = "https://generativelanguage.googleapis.com"
_API_VERSION = "v1beta"

# The provider rejects a raw pydantic JSON Schema: `$defs`, `$ref`, and
# `additionalProperties` are not fields of its OpenAPI-subset Schema message. Only the
# keywords below survive the transform, and `const` is rewritten as a single-value enum.
_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "description",
        "enum",
        "format",
        "items",
        "maximum",
        "minimum",
        "properties",
        "required",
        "type",
    }
)

# The project scale has no `minimal`, and this model cannot turn thinking off, so the
# lowest representable effort maps to the provider floor instead of claiming `none`.
_THINKING_LEVELS: Mapping[str, str] = {
    "low": "minimal",
    "medium": "medium",
    "high": "high",
}


class GeminiContractError(ValueError):
    """A payload-free error raised before any hosted request is attempted."""

    def __init__(self, safe_error_code: str = "GEMINI_CONTRACT_REJECTED") -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class GeminiTokenCountError(RuntimeError):
    """A bounded count failure that never retains a response body or request payload."""

    def __init__(self, safe_error_code: str) -> None:
        self.safe_error_code = safe_error_code
        super().__init__(safe_error_code)


class GeminiHttpStatusError(RuntimeError):
    """A non-200 provider status carried without its response body."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"GEMINI_HTTP_{status_code}")


def to_gemini_schema(node: object, definitions: Mapping[str, Any]) -> Any:
    """Project a pydantic JSON Schema onto the provider's accepted schema subset."""

    if not isinstance(node, dict):
        return node
    reference = node.get("$ref")
    if isinstance(reference, str):
        return to_gemini_schema(definitions.get(reference.rsplit("/", 1)[-1], {}), definitions)

    projected: dict[str, Any] = {}
    for key, value in node.items():
        if key == "const":
            projected["enum"] = [value]
            continue
        if key not in _SUPPORTED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            projected[key] = {
                name: to_gemini_schema(item, definitions) for name, item in value.items()
            }
        elif key == "items":
            projected[key] = to_gemini_schema(value, definitions)
        else:
            projected[key] = value
    if "enum" in projected and "type" not in projected:
        # The provider ignores an enum whose type is absent, which silently drops the
        # only constraint that keeps routing output inside the closed intent set.
        projected["type"] = "string"
    return projected


@dataclass(frozen=True, slots=True, repr=False)
class _WireMessage:
    role: Literal["user", "model"]
    content: str


@dataclass(frozen=True, slots=True, repr=False)
class GeminiGenerateContentWire:
    """Compiled immutable request material shared by counting and generation."""

    model_snapshot: str
    system_instruction: str
    messages: tuple[_WireMessage, ...]
    response_schema_json: bytes
    thinking_level: str
    max_output_tokens: int

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    __str__ = __repr__

    def request_body(self) -> dict[str, Any]:
        return {
            "contents": [
                {"role": message.role, "parts": [{"text": message.content}]}
                for message in self.messages
            ],
            "generationConfig": {
                "maxOutputTokens": self.max_output_tokens,
                "responseMimeType": "application/json",
                "responseSchema": json.loads(self.response_schema_json),
                "thinkingConfig": {"thinkingLevel": self.thinking_level},
            },
            "systemInstruction": {"parts": [{"text": self.system_instruction}]},
        }

    def count_body(self) -> dict[str, Any]:
        # Counting the whole request, not just `contents`, is what makes the preflight
        # count equal to the prompt tokens the provider later reports and bills.
        return {
            "generateContentRequest": {
                "model": f"models/{self.model_snapshot}",
                **self.request_body(),
            }
        }

    def canonical_shared_bytes(self) -> bytes:
        return json.dumps(
            self.request_body(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


def compile_gemini_wire[ResultT: BaseModel](
    invocation: ModelInvocation[ResultT],
    *,
    contract: HostedModelContract = GEMINI_3_1_FLASH_LITE_CONTRACT,
) -> GeminiGenerateContentWire:
    """Validate the pinned contract and compile the only allowed provider wire."""

    if invocation.profile != contract.model_profile():
        raise GeminiContractError("GEMINI_PROFILE_MISMATCH")
    if invocation.structured_output_mode != StructuredOutputMode.JSON_SCHEMA:
        raise GeminiContractError("GEMINI_OUTPUT_MODE_UNSUPPORTED")
    if invocation.response_model is not StructuredIntentDecision:
        raise GeminiContractError("GEMINI_RESPONSE_SCHEMA_UNSUPPORTED")
    if invocation.provider_options:
        # Secrets, endpoint overrides, retries, tools, and sampling options are all denied.
        raise GeminiContractError("GEMINI_PROVIDER_OPTIONS_UNSUPPORTED")
    if invocation.max_output_tokens < contract.minimum_application_output_tokens:
        raise GeminiContractError("GEMINI_OUTPUT_RESERVE_TOO_SMALL")
    if invocation.max_output_tokens > contract.hard_max_output_tokens:
        raise GeminiContractError("GEMINI_OUTPUT_RESERVE_TOO_LARGE")
    thinking_level = _THINKING_LEVELS.get(contract.reasoning_effort)
    if thinking_level is None:
        raise GeminiContractError("GEMINI_REASONING_EFFORT_UNSUPPORTED")

    raw_schema = invocation.response_model.model_json_schema(mode="validation")
    schema_bytes = json.dumps(
        to_gemini_schema(raw_schema, raw_schema.get("$defs", {})),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    messages = tuple(
        _WireMessage(
            role=("user" if message.role == ModelMessageRole.USER else "model"),
            content=message.content,
        )
        for message in invocation.messages
    )
    return GeminiGenerateContentWire(
        model_snapshot=contract.provider_model_snapshot,
        system_instruction=invocation.system_policy,
        messages=messages,
        response_schema_json=schema_bytes,
        thinking_level=thinking_level,
        max_output_tokens=invocation.max_output_tokens,
    )


class GeminiTransport(Protocol):
    async def count_tokens(self, wire: GeminiGenerateContentWire) -> Mapping[str, Any]: ...

    async def generate(self, wire: GeminiGenerateContentWire) -> Mapping[str, Any]: ...

    async def aclose(self) -> None: ...


class GeminiHttpTransport:
    """Fixed-origin REST transport with a bounded timeout and no retries."""

    __slots__ = ("_client",)

    def __init__(
        self,
        *,
        api_key: SecretStr,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        secret = api_key.get_secret_value()
        if not secret:
            raise ValueError("GEMINI_API_KEY_REQUIRED")
        if not (0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS):
            raise ValueError("GEMINI_TIMEOUT_INVALID")
        # Only the transport is injectable, so the fixed origin, the header-carried key,
        # and the zero-retry policy stay under test instead of being replaceable.
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            # The key travels in a header so it never lands in a URL, proxy log, or referrer.
            headers={"x-goog-api-key": secret, "content-type": "application/json"},
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0)),
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    async def _post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        response = await self._client.post(path, json=body)
        if response.status_code != httpx.codes.OK:
            raise GeminiHttpStatusError(response.status_code)
        payload = response.json()
        if not isinstance(payload, dict):
            raise GeminiHttpStatusError(response.status_code)
        return payload

    async def count_tokens(self, wire: GeminiGenerateContentWire) -> Mapping[str, Any]:
        return await self._post(
            f"/{_API_VERSION}/models/{wire.model_snapshot}:countTokens",
            wire.count_body(),
        )

    async def generate(self, wire: GeminiGenerateContentWire) -> Mapping[str, Any]:
        return await self._post(
            f"/{_API_VERSION}/models/{wire.model_snapshot}:generateContent",
            wire.request_body(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class GeminiStructuredModel:
    """Evaluation-ready adapter; production graph activation is intentionally separate."""

    def __init__(
        self,
        transport: GeminiTransport,
        *,
        contract: HostedModelContract = GEMINI_3_1_FLASH_LITE_CONTRACT,
    ) -> None:
        self._transport = transport
        self._contract = contract

    @classmethod
    def from_api_key(
        cls,
        api_key: SecretStr,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        contract: HostedModelContract = GEMINI_3_1_FLASH_LITE_CONTRACT,
    ) -> GeminiStructuredModel:
        return cls(
            GeminiHttpTransport(api_key=api_key, timeout_seconds=timeout_seconds),
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
        wire = compile_gemini_wire(invocation, contract=self._contract)
        payload: Mapping[str, Any] | None = None
        error_code: str | None = None
        try:
            payload = await self._transport.count_tokens(wire)
        except Exception as error:
            error_code = _count_error_code(error)
        if error_code is not None:
            # Raising outside the handler keeps the provider exception, and any body it
            # references, off the exception chain of the error the caller observes.
            raise GeminiTokenCountError(error_code)
        total = payload.get("totalTokens") if payload is not None else None
        if type(total) is not int or total < 0:
            raise GeminiTokenCountError("GEMINI_COUNT_INVALID")
        return TokenCount(
            input_tokens=total,
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
            wire = compile_gemini_wire(invocation, contract=self._contract)
        except GeminiContractError as error:
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
                safe_error_code="GEMINI_PREFLIGHT_MISMATCH",
            )

        try:
            body = await self._transport.generate(wire)
        except Exception as error:
            kind, code = _invocation_error(error)
            return ModelFailure(kind=kind, safe_error_code=code)

        usage = _normalized_usage(
            body,
            estimated_input_tokens=preflight.input_tokens,
            estimated_output_tokens=invocation.max_output_tokens,
            estimator_version=preflight.counter_version,
        )
        if body.get("modelVersion") != self._contract.provider_model_snapshot:
            return ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                usage=usage,
                safe_error_code="GEMINI_MODEL_IDENTITY_MISMATCH",
            )
        if usage.output_tokens > invocation.max_output_tokens:
            # Thought tokens are billed as output, so the reserve must bound their sum.
            return ModelFailure(
                kind=ModelFailureKind.PROVIDER_ERROR,
                usage=usage,
                safe_error_code="GEMINI_OUTPUT_CAP_MISMATCH",
            )
        prompt_feedback = body.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            return ModelFailure(
                kind=ModelFailureKind.INVALID_STRUCTURED_OUTPUT,
                usage=usage,
                safe_error_code="GEMINI_PROMPT_BLOCKED",
            )

        candidates = body.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 1:
            return ModelFailure(
                kind=ModelFailureKind.INVALID_STRUCTURED_OUTPUT,
                usage=usage,
                safe_error_code="GEMINI_CANDIDATE_CARDINALITY_INVALID",
            )
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason") if isinstance(candidate, dict) else None
        if finish_reason != "STOP":
            return ModelFailure(
                kind=(
                    ModelFailureKind.INVALID_STRUCTURED_OUTPUT
                    if finish_reason in {"MAX_TOKENS", "SAFETY", "RECITATION"}
                    else ModelFailureKind.PROVIDER_ERROR
                ),
                usage=usage,
                safe_error_code=(
                    "GEMINI_RESPONSE_INCOMPLETE"
                    if finish_reason == "MAX_TOKENS"
                    else (
                        "GEMINI_RESPONSE_BLOCKED"
                        if finish_reason in {"SAFETY", "RECITATION"}
                        else "GEMINI_RESPONSE_NOT_COMPLETED"
                    )
                ),
            )
        output_text = _single_output_text(candidate)
        if output_text is None:
            return ModelFailure(
                kind=ModelFailureKind.INVALID_STRUCTURED_OUTPUT,
                usage=usage,
                safe_error_code="GEMINI_OUTPUT_CARDINALITY_INVALID",
            )
        try:
            value = invocation.response_model.model_validate_json(output_text, strict=True)
        except (ValidationError, ValueError, TypeError):
            return ModelFailure(
                kind=ModelFailureKind.INVALID_STRUCTURED_OUTPUT,
                usage=usage,
                safe_error_code="GEMINI_STRUCTURED_OUTPUT_INVALID",
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


def _reported_int(usage: Mapping[str, Any], key: str) -> int | None:
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else None


def _normalized_usage(
    body: Mapping[str, Any],
    *,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    estimator_version: str,
) -> ModelUsage:
    raw = body.get("usageMetadata")
    usage: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    visible = _reported_int(usage, "candidatesTokenCount")
    thoughts = _reported_int(usage, "thoughtsTokenCount")
    # The provider reports thoughts separately but bills them at the output rate, so the
    # charged output is their sum rather than the visible candidate tokens alone.
    billed_output = None if visible is None else visible + (thoughts or 0)
    return normalize_model_usage(
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        estimator_version=estimator_version,
        reported_input_tokens=_reported_int(usage, "promptTokenCount"),
        reported_output_tokens=billed_output,
        reported_total_tokens=_reported_int(usage, "totalTokenCount"),
        reported_cached_input_tokens=_reported_int(usage, "cachedContentTokenCount"),
        reported_reasoning_output_tokens=thoughts,
    )


def _single_output_text(candidate: Mapping[str, Any]) -> str | None:
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return None
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and type(part.get("text")) is str and not part.get("thought")
    ]
    return texts[0] if len(texts) == 1 and texts[0] else None


def _count_error_code(error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "GEMINI_COUNT_TIMEOUT"
    if isinstance(error, httpx.TransportError):
        return "GEMINI_COUNT_TRANSPORT"
    return "GEMINI_COUNT_PROVIDER_ERROR"


def _invocation_error(error: Exception) -> tuple[ModelFailureKind, str]:
    if isinstance(error, httpx.TimeoutException):
        return ModelFailureKind.TIMEOUT, "GEMINI_REQUEST_TIMEOUT"
    if isinstance(error, httpx.TransportError):
        return ModelFailureKind.TRANSPORT, "GEMINI_REQUEST_TRANSPORT"
    if isinstance(error, GeminiHttpStatusError):
        return ModelFailureKind.PROVIDER_ERROR, "GEMINI_PROVIDER_ERROR"
    return ModelFailureKind.PROVIDER_ERROR, "GEMINI_PROVIDER_MALFORMED"
