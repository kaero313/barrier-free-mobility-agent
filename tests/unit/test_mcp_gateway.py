from __future__ import annotations

import asyncio
import gzip
import traceback
from dataclasses import dataclass
from datetime import timedelta
from types import TracebackType
from typing import Any, Self, cast

import httpx
import pytest
import respx
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, Tool
from tests.factories import facility_question_result, mobility_profile, trip_question_result

from barrier_free_mobility_agent.config import McpAuthMode, Settings
from barrier_free_mobility_agent.domain.errors import (
    McpContractError,
    McpDependencyDegradedError,
    McpResponseValidationError,
    McpTimeoutError,
    McpToolExecutionError,
    McpUnavailableError,
)
from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    AccessibilityResult,
    ContractStatus,
    McpCapability,
    MobilityProfile,
    StationResolutionResult,
)
from barrier_free_mobility_agent.integrations.mcp import gateway as gateway_module
from barrier_free_mobility_agent.integrations.mcp.gateway import LangChainMcpGateway
from barrier_free_mobility_agent.integrations.mcp.payload_limits import (
    MCP_PAYLOAD_MAX_STRING_CHARS,
    McpResponseBodyLimitError,
    enforce_mcp_response_body_limit,
)


@dataclass
class FakePage:
    tools: list[Tool]
    nextCursor: str | None = None


@dataclass
class FakeCallResult:
    isError: bool
    structuredContent: dict[str, Any] | None


class FakeSession:
    def __init__(
        self,
        *,
        tools: list[Tool],
        call_result: FakeCallResult | None = None,
        call_results: dict[str, FakeCallResult] | None = None,
        call_exception: BaseException | None = None,
        list_exception: BaseException | None = None,
        pages: list[FakePage] | None = None,
    ) -> None:
        self.tools = tools
        self.call_result = call_result or FakeCallResult(
            isError=False,
            structuredContent=trip_question_result().model_dump(mode="json"),
        )
        self.call_results = call_results or {}
        self.call_exception = call_exception
        self.list_exception = list_exception
        self.pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.list_calls: list[str | None] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def list_tools(self, *, cursor: str | None = None) -> FakePage:
        self.list_calls.append(cursor)
        if self.list_exception is not None:
            raise self.list_exception
        if self.pages is not None:
            index = min(len(self.list_calls) - 1, len(self.pages) - 1)
            return self.pages[index]
        return FakePage(tools=self.tools)

    async def call_tool(
        self,
        name: str,
        *,
        arguments: dict[str, Any],
        read_timeout_seconds: timedelta,
    ) -> FakeCallResult:
        self.calls.append((name, arguments))
        if self.call_exception is not None:
            raise self.call_exception
        return self.call_results.get(name, self.call_result)


class FakeClient:
    def __init__(self, session: FakeSession) -> None:
        self.session_instance = session

    def session(self, server_name: str) -> FakeSession:
        return self.session_instance


def answer_tool() -> Tool:
    return Tool(
        name="answer_accessibility_question",
        inputSchema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
        outputSchema=AccessibilityQuestionResult.model_json_schema(),
    )


def resolve_station_tool() -> Tool:
    return Tool(
        name="resolve_station",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        outputSchema=StationResolutionResult.model_json_schema(),
    )


def generate_accessibility_brief_tool() -> Tool:
    profile_schema = MobilityProfile.model_json_schema()
    profile_definitions = profile_schema.pop("$defs")
    return Tool(
        name="generate_accessibility_brief",
        inputSchema={
            "type": "object",
            "$defs": profile_definitions,
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "mobility_profile": profile_schema,
            },
            "required": ["origin", "destination", "mobility_profile"],
        },
        outputSchema=AccessibilityResult.model_json_schema(),
    )


def required_tools() -> list[Tool]:
    return [answer_tool(), resolve_station_tool(), generate_accessibility_brief_tool()]


def health_payload(*, max_chars: int = 120) -> dict[str, Any]:
    return {
        "status": "ok",
        "mcp": {
            "tool_input_max_chars": max_chars,
            "auth_enabled": False,
        },
    }


async def initialized_gateway(
    settings: Settings,
    *,
    call_result: FakeCallResult | None = None,
    call_results: dict[str, FakeCallResult] | None = None,
    call_exception: BaseException | None = None,
) -> LangChainMcpGateway:
    gateway = LangChainMcpGateway(settings)
    gateway.__dict__["_client"] = FakeClient(
        FakeSession(
            tools=required_tools(),
            call_result=call_result,
            call_results=call_results,
            call_exception=call_exception,
        )
    )
    with respx.mock(assert_all_called=True) as router:
        router.get(settings.mcp_mobility_health_url).mock(
            return_value=httpx.Response(200, json=health_payload())
        )
        await gateway.initialize()
    return gateway


@pytest.mark.asyncio
async def test_initialize_discovers_and_fingerprints_contract(settings: Settings) -> None:
    gateway = await initialized_gateway(settings)
    try:
        snapshot = gateway.contract_snapshot

        assert snapshot.status == ContractStatus.READY
        assert snapshot.tool_names == (
            "answer_accessibility_question",
            "generate_accessibility_brief",
            "resolve_station",
        )
        assert snapshot.schema_fingerprint is not None
        assert len(snapshot.schema_fingerprint) == 64
        assert snapshot.capabilities == (
            McpCapability.TRIP_ACCESSIBILITY,
            McpCapability.FACILITY_QUESTION,
        )
        assert await gateway.refresh_if_stale() == snapshot
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_facility_wrapper_uses_composite_tool_and_validates_intent(
    settings: Settings,
) -> None:
    expected = facility_question_result()
    gateway = await initialized_gateway(
        settings,
        call_result=FakeCallResult(
            isError=False,
            structuredContent=expected.model_dump(mode="json"),
        ),
    )
    try:
        result = await gateway.answer_facility_question("강남역 엘리베이터 상태")
        client = cast(FakeClient, gateway._client)

        assert result.intent == "facility_status"
        assert client.session_instance.calls[-1] == (
            "answer_accessibility_question",
            {"question": "강남역 엘리베이터 상태"},
        )
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_facility_wrapper_rejects_semantically_wrong_intent(settings: Settings) -> None:
    gateway = await initialized_gateway(settings)
    try:
        with pytest.raises(McpResponseValidationError) as captured:
            await gateway.answer_facility_question("강남역 엘리베이터 상태")

        assert captured.value.context["reason"] == "facility_intent_mismatch"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_initialize_fails_closed_when_phase2_tool_is_missing(settings: Settings) -> None:
    gateway = LangChainMcpGateway(settings)
    gateway.__dict__["_client"] = FakeClient(FakeSession(tools=[answer_tool()]))
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(settings.mcp_mobility_health_url).mock(
                return_value=httpx.Response(200, json=health_payload())
            )
            with pytest.raises(McpContractError):
                await gateway.initialize()

        assert gateway.contract_snapshot.status == ContractStatus.INCOMPATIBLE
        assert gateway.contract_snapshot.error_code == "resolve_station_tool_missing"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_resolve_station_returns_typed_result_and_exact_arguments(
    settings: Settings,
) -> None:
    payload = {
        "query": "홍대입구",
        "matched_station": {
            "station_id": "ST-001",
            "station_name": "홍대입구",
            "line": "2호선",
            "aliases": ["홍대"],
            "confidence": 0.98,
        },
        "candidates": [],
        "needs_clarification": False,
        "clarification_message": None,
    }
    gateway = await initialized_gateway(
        settings,
        call_results={"resolve_station": FakeCallResult(isError=False, structuredContent=payload)},
    )
    try:
        result = await gateway.resolve_station("홍대입구")
        client = cast(FakeClient, gateway._client)

        assert isinstance(result, StationResolutionResult)
        assert result.matched_station is not None
        assert result.matched_station.station_id == "ST-001"
        assert client.session_instance.calls[-1] == (
            "resolve_station",
            {"query": "홍대입구"},
        )
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_generate_brief_returns_typed_result_and_serializes_profile(
    settings: Settings,
) -> None:
    expected = trip_question_result().result
    assert expected is not None
    profile = MobilityProfile.model_validate(mobility_profile())
    gateway = await initialized_gateway(
        settings,
        call_results={
            "generate_accessibility_brief": FakeCallResult(
                isError=False,
                structuredContent=expected.model_dump(mode="json"),
            )
        },
    )
    try:
        result = await gateway.generate_accessibility_brief("홍대입구", "삼성", profile)
        client = cast(FakeClient, gateway._client)

        assert isinstance(result, AccessibilityResult)
        assert result.user_message == expected.user_message
        assert client.session_instance.calls[-1] == (
            "generate_accessibility_brief",
            {
                "origin": "홍대입구",
                "destination": "삼성",
                "mobility_profile": profile.model_dump(mode="json"),
            },
        )
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_health_contract_mismatch_marks_gateway_incompatible(
    settings: Settings,
) -> None:
    gateway = LangChainMcpGateway(settings)
    gateway.__dict__["_client"] = FakeClient(FakeSession(tools=[answer_tool()]))
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(settings.mcp_mobility_health_url).mock(
                return_value=httpx.Response(200, json=health_payload(max_chars=121))
            )
            with pytest.raises(McpContractError):
                await gateway.initialize()

        assert gateway.contract_snapshot.status == ContractStatus.INCOMPATIBLE
        assert gateway.contract_snapshot.error_code == "tool_input_limit_mismatch"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_degraded_health_has_distinct_snapshot_status(settings: Settings) -> None:
    gateway = LangChainMcpGateway(settings)
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(settings.mcp_mobility_health_url).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": "degraded",
                        "mcp": {
                            "tool_input_max_chars": 120,
                            "auth_enabled": False,
                        },
                    },
                )
            )
            with pytest.raises(McpDependencyDegradedError):
                await gateway.initialize()

        assert gateway.contract_snapshot.status == ContractStatus.DEGRADED
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_health_transport_error_marks_gateway_unavailable(settings: Settings) -> None:
    gateway = LangChainMcpGateway(settings)
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(settings.mcp_mobility_health_url).mock(
                side_effect=httpx.ConnectError("offline")
            )
            with pytest.raises(McpUnavailableError):
                await gateway.initialize()

        assert gateway.contract_snapshot.status == ContractStatus.UNAVAILABLE
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_health_timeout_is_classified_separately(settings: Settings) -> None:
    gateway = LangChainMcpGateway(settings)
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(settings.mcp_mobility_health_url).mock(side_effect=httpx.ReadTimeout("slow"))
            with pytest.raises(McpTimeoutError):
                await gateway.initialize()

        assert gateway.contract_snapshot.error_code == "MCP_TIMEOUT"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_oversized_health_body_fails_closed_before_json_decode(
    settings: Settings,
) -> None:
    gateway = LangChainMcpGateway(settings)
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(settings.mcp_mobility_health_url).mock(
                return_value=httpx.Response(
                    200,
                    content=b"x" * (gateway_module._HEALTH_RESPONSE_MAX_BYTES + 1),
                )
            )
            with pytest.raises(McpContractError) as captured:
                await gateway.initialize()

        assert captured.value.context == {"reason": "health_response_bytes_limit"}
        assert gateway.contract_snapshot.status == ContractStatus.INCOMPATIBLE
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_compressed_health_body_is_rejected_before_decode(settings: Settings) -> None:
    gateway = LangChainMcpGateway(settings)
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(settings.mcp_mobility_health_url).mock(
                return_value=httpx.Response(
                    200,
                    content=gzip.compress(b"compressed"),
                    headers={"content-encoding": "gzip"},
                )
            )
            with pytest.raises(McpContractError) as captured:
                await gateway.initialize()

        assert captured.value.context == {"reason": "health_response_compression_unsupported"}
        assert gateway.contract_snapshot.status == ContractStatus.INCOMPATIBLE
        assert gateway._health_client.headers["accept-encoding"] == "identity"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_tool_discovery_rejects_cursor_cycles(settings: Settings) -> None:
    gateway = LangChainMcpGateway(settings)
    session = FakeSession(
        tools=[],
        pages=[
            FakePage(tools=[answer_tool()], nextCursor="repeat"),
            FakePage(tools=[], nextCursor="repeat"),
        ],
    )
    gateway.__dict__["_client"] = FakeClient(session)
    try:
        with pytest.raises(McpContractError) as captured:
            await gateway._list_tools()

        assert captured.value.context == {"reason": "tool_cursor_cycle"}
        assert session.list_calls == [None, "repeat"]
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_tool_discovery_rejects_count_and_schema_limits(settings: Settings) -> None:
    too_many = [
        Tool(name=f"tool-{index}", inputSchema={"type": "object"})
        for index in range(gateway_module._TOOL_DISCOVERY_MAX_TOOLS + 1)
    ]
    count_gateway = LangChainMcpGateway(settings)
    count_gateway.__dict__["_client"] = FakeClient(FakeSession(tools=too_many))
    try:
        with pytest.raises(McpContractError) as count_error:
            await count_gateway._list_tools()
        assert count_error.value.context == {"reason": "tool_count_limit_exceeded"}
    finally:
        await count_gateway.close()

    schema_gateway = LangChainMcpGateway(settings)
    schema_gateway.__dict__["_client"] = FakeClient(
        FakeSession(
            tools=[
                Tool(
                    name="oversized-schema",
                    description="민감한 설명" * (MCP_PAYLOAD_MAX_STRING_CHARS + 1),
                    inputSchema={"type": "object"},
                )
            ]
        )
    )
    try:
        with pytest.raises(McpContractError) as schema_error:
            await schema_gateway._list_tools()
        assert schema_error.value.context == {"reason": "tool_schema_string_chars_limit"}
    finally:
        await schema_gateway.close()


@pytest.mark.asyncio
async def test_tool_error_is_not_returned_as_model_content(settings: Settings) -> None:
    gateway = await initialized_gateway(
        settings,
        call_result=FakeCallResult(isError=True, structuredContent=None),
    )
    try:
        with pytest.raises(McpToolExecutionError):
            await gateway.answer_accessibility_question("휠체어로 갈 수 있어?")
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_missing_structured_content_fails_closed(settings: Settings) -> None:
    gateway = await initialized_gateway(
        settings,
        call_result=FakeCallResult(isError=False, structuredContent=None),
    )
    try:
        with pytest.raises(McpResponseValidationError):
            await gateway.answer_accessibility_question("휠체어로 갈 수 있어?")

        assert gateway.contract_snapshot.status == ContractStatus.INCOMPATIBLE
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_invalid_dto_does_not_retain_private_payload_in_exception_chain(
    settings: Settings,
) -> None:
    private_value = "비공개-사용자-위치"
    gateway = await initialized_gateway(
        settings,
        call_result=FakeCallResult(
            isError=False,
            structuredContent={"question": private_value},
        ),
    )
    try:
        with pytest.raises(McpResponseValidationError) as captured:
            await gateway.answer_accessibility_question("휠체어로 갈 수 있어?")

        formatted = "".join(
            traceback.format_exception(
                captured.type,
                captured.value,
                captured.tb,
            )
        )
        assert captured.value.__cause__ is None
        assert private_value not in formatted
        assert captured.value.detail is None
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_oversized_structured_content_fails_before_dto_validation(
    settings: Settings,
) -> None:
    private_value = "비공개위치" * (MCP_PAYLOAD_MAX_STRING_CHARS + 1)
    gateway = await initialized_gateway(
        settings,
        call_result=FakeCallResult(
            isError=False,
            structuredContent={"question": private_value},
        ),
    )
    try:
        with pytest.raises(McpResponseValidationError) as captured:
            await gateway.answer_accessibility_question("휠체어로 갈 수 있어?")

        assert captured.value.context == {
            "reason": "structured_content_limit_exceeded",
            "limit_kind": "string_chars",
            "tool": "answer_accessibility_question",
        }
        assert private_value not in str(captured.value)
        assert gateway.contract_snapshot.status == ContractStatus.INCOMPATIBLE
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_tool_timeout_marks_gateway_unavailable(settings: Settings) -> None:
    gateway = await initialized_gateway(settings, call_exception=TimeoutError("slow"))
    try:
        with pytest.raises(McpTimeoutError):
            await gateway.answer_accessibility_question("휠체어로 갈 수 있어?")

        assert gateway.contract_snapshot.status == ContractStatus.UNAVAILABLE
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [
        ExceptionGroup("transport", [httpx.ReadTimeout("slow")]),
        McpError(ErrorData(code=408, message="request timed out")),
    ],
)
async def test_wrapped_tool_timeout_is_classified_without_retaining_transport_error(
    settings: Settings,
    transport_error: BaseException,
) -> None:
    gateway = await initialized_gateway(settings, call_exception=transport_error)
    try:
        with pytest.raises(McpTimeoutError) as captured:
            await gateway.answer_accessibility_question("Can I travel between these stations?")

        assert captured.value.__cause__ is None
        assert gateway.contract_snapshot.status == ContractStatus.UNAVAILABLE
        assert gateway.contract_snapshot.error_code == McpTimeoutError.code
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_wrapped_tool_body_limit_is_classified_as_contract_incompatible(
    settings: Settings,
) -> None:
    transport_error = ExceptionGroup(
        "transport",
        [McpResponseBodyLimitError("body_bytes")],
    )
    gateway = await initialized_gateway(settings, call_exception=transport_error)
    try:
        with pytest.raises(McpResponseValidationError) as captured:
            await gateway.answer_accessibility_question("Can I travel between these stations?")

        assert captured.value.__cause__ is None
        assert captured.value.context["reason"] == "mcp_response_body_limit"
        assert gateway.contract_snapshot.status == ContractStatus.INCOMPATIBLE
        assert gateway.contract_snapshot.error_code == "mcp_response_body_limit"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_wrapped_initialize_timeout_is_classified_as_timeout(settings: Settings) -> None:
    gateway = LangChainMcpGateway(settings)
    gateway.__dict__["_client"] = FakeClient(
        FakeSession(
            tools=required_tools(),
            list_exception=ExceptionGroup("transport", [httpx.ReadTimeout("slow")]),
        )
    )
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(settings.mcp_mobility_health_url).mock(
                return_value=httpx.Response(200, json=health_payload())
            )
            with pytest.raises(McpTimeoutError) as captured:
                await gateway.initialize()

        assert captured.value.__cause__ is None
        assert gateway.contract_snapshot.status == ContractStatus.UNAVAILABLE
        assert gateway.contract_snapshot.error_code == McpTimeoutError.code
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_mcp_http_client_requests_identity_encoding_and_keeps_response_hook() -> None:
    client = gateway_module._bounded_mcp_http_client(
        headers={"accept-encoding": "gzip", "X-Test": "kept"}
    )
    try:
        assert client.headers["accept-encoding"] == "identity"
        assert client.headers["x-test"] == "kept"
        assert client.event_hooks["response"] == [enforce_mcp_response_body_limit]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mcp_http_client_bounds_best_effort_session_delete() -> None:
    delete_started = asyncio.Event()
    delete_cancelled = asyncio.Event()

    async def slow_delete(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        delete_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            delete_cancelled.set()
            raise
        raise AssertionError("The DELETE handler must be cancelled by its cleanup deadline.")

    client = gateway_module._bounded_mcp_http_client(transport=httpx.MockTransport(slow_delete))
    started_at = asyncio.get_running_loop().time()
    try:
        with pytest.raises(TimeoutError):
            await client.delete("https://mcp.example/mcp")

        assert asyncio.get_running_loop().time() - started_at < 1.0
        assert delete_started.is_set()
        assert delete_cancelled.is_set()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cancellation_is_propagated(settings: Settings) -> None:
    gateway = await initialized_gateway(
        settings,
        call_exception=asyncio.CancelledError(),
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await gateway.answer_accessibility_question("휠체어로 갈 수 있어?")
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_oidc_auth_is_shared_by_mcp_and_health_clients() -> None:
    settings = Settings(
        _env_file=None,
        mcp_mobility_auth_mode=McpAuthMode.OIDC,
        mcp_mobility_oidc_token_url="https://identity.example.com/oauth/token",
        mcp_mobility_oidc_client_id="mobility-agent",
        mcp_mobility_oidc_client_secret="client-secret",
    )
    gateway = LangChainMcpGateway(settings)
    try:
        connection = cast(
            StreamableHttpConnection,
            gateway._client.connections["mobility"],
        )

        assert connection["auth"] is gateway._oidc_auth
        assert gateway._health_client._auth is gateway._oidc_auth
        assert connection["headers"] == {}
    finally:
        await gateway.close()
