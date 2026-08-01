from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from time import perf_counter

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION, Tool
from tests.factories import trip_question_result

from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.errors import (
    McpContractError,
    McpResponseValidationError,
    McpUnavailableError,
)
from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    AccessibilityResult,
    ContractStatus,
    MobilityProfile,
    StationResolutionResult,
)
from barrier_free_mobility_agent.integrations.mcp import gateway as gateway_module
from barrier_free_mobility_agent.integrations.mcp.gateway import LangChainMcpGateway
from barrier_free_mobility_agent.integrations.mcp.payload_limits import (
    MCP_HTTP_RESPONSE_MAX_BYTES,
)


class ChunkedResponseStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.yielded_chunks = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.yielded_chunks += 1
            yield chunk


def oversized_initialization_stream(content_type: str) -> ChunkedResponseStream:
    padding = b"x" * (MCP_HTTP_RESPONSE_MAX_BYTES // 2 + 1)
    prefix = (
        b'data: {"jsonrpc":"2.0","id":0,"result":{"padding":"'
        if content_type == "text/event-stream"
        else b'{"jsonrpc":"2.0","id":0,"result":{"padding":"'
    )
    return ChunkedResponseStream((prefix + padding, padding, b'"}}\n\n'))


def install_mock_mcp_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
) -> None:
    original_factory = gateway_module._bounded_mcp_http_client

    def mock_client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return original_factory(
            headers=headers,
            timeout=timeout,
            auth=auth,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(gateway_module, "_bounded_mcp_http_client", mock_client_factory)


async def replace_health_client(gateway: LangChainMcpGateway) -> None:
    original_health_client = gateway.__dict__["_health_client"]
    assert isinstance(original_health_client, httpx.AsyncClient)
    await original_health_client.aclose()

    def health_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "mcp": {
                    "tool_input_max_chars": 120,
                    "auth_enabled": False,
                },
            },
        )

    gateway.__dict__["_health_client"] = httpx.AsyncClient(
        transport=httpx.MockTransport(health_handler)
    )


def required_tools_payload() -> list[dict[str, object]]:
    profile_schema = MobilityProfile.model_json_schema()
    profile_definitions = profile_schema.pop("$defs")
    tools = (
        Tool(
            name="answer_accessibility_question",
            inputSchema={
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
            outputSchema=AccessibilityQuestionResult.model_json_schema(),
        ),
        Tool(
            name="resolve_station",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            outputSchema=StationResolutionResult.model_json_schema(),
        ),
        Tool(
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
        ),
    )
    return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]


def json_rpc_response(
    request_id: str | int,
    result: dict[str, object],
    *,
    session_id: str = "test-session",
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"mcp-session-id": session_id},
        json={"jsonrpc": "2.0", "id": request_id, "result": result},
    )


class SlowDeleteToolHandler:
    def __init__(self, *, json_rpc_error: bool) -> None:
        self.json_rpc_error = json_rpc_error
        self.tool_response_sent = False
        self.delete_started = asyncio.Event()
        self.delete_cancelled = asyncio.Event()
        self.delete_completed = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            if self.tool_response_sent:
                self.delete_started.set()
                try:
                    await asyncio.sleep(2.0)
                except asyncio.CancelledError:
                    self.delete_cancelled.set()
                    raise
                self.delete_completed = True
            return httpx.Response(200)

        message = json.loads(request.content)
        method = message["method"]
        if method == "initialize":
            return json_rpc_response(
                message["id"],
                {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "slow-delete-mcp", "version": "1.0.0"},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return json_rpc_response(message["id"], {"tools": required_tools_payload()})
        if method == "tools/call":
            self.tool_response_sent = True
            if self.json_rpc_error:
                return httpx.Response(
                    200,
                    headers={
                        "content-type": "application/json",
                        "mcp-session-id": "test-session",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32603,
                            "message": "simulated tool failure",
                        },
                    },
                )
            return json_rpc_response(
                message["id"],
                {
                    "content": [],
                    "structuredContent": trip_question_result().model_dump(mode="json"),
                    "isError": False,
                },
            )
        raise AssertionError(f"Unexpected MCP method: {method}")


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
@pytest.mark.asyncio
async def test_actual_mcp_sdk_transport_latches_oversized_initialize_response(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    content_type: str,
) -> None:
    streams: list[ChunkedResponseStream] = []
    requests: list[httpx.Request] = []

    async def mcp_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        stream = oversized_initialization_stream(content_type)
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": content_type},
            stream=stream,
        )

    install_mock_mcp_transport(monkeypatch, mcp_handler)
    gateway = LangChainMcpGateway(settings)
    await replace_health_client(gateway)
    started_at = perf_counter()

    try:
        with pytest.raises(McpContractError) as captured:
            async with asyncio.timeout(2.0):
                await gateway.initialize()

        assert perf_counter() - started_at < 1.5
        assert captured.value.context == {"reason": "mcp_response_body_limit"}
        assert gateway.contract_snapshot.status == ContractStatus.INCOMPATIBLE
        assert gateway.contract_snapshot.error_code == "mcp_response_body_limit"
        assert streams
        assert streams[0].yielded_chunks == 2
        assert requests[0].method == "POST"
        assert b'"method":"initialize"' in requests[0].content
    finally:
        await gateway.close()


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
@pytest.mark.asyncio
async def test_actual_mcp_sdk_tool_overflow_does_not_wait_for_slow_delete(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    content_type: str,
) -> None:
    overflow_streams: list[ChunkedResponseStream] = []
    requested_methods: list[str] = []
    overflow_sent = False
    slow_delete_started = False
    slow_delete_completed = False

    async def mcp_handler(request: httpx.Request) -> httpx.Response:
        nonlocal overflow_sent, slow_delete_completed, slow_delete_started
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            if overflow_sent:
                slow_delete_started = True
                await asyncio.sleep(2.0)
                slow_delete_completed = True
            return httpx.Response(200)

        message = json.loads(request.content)
        method = message["method"]
        requested_methods.append(method)
        if method == "initialize":
            return json_rpc_response(
                message["id"],
                {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "bounded-test-mcp", "version": "1.0.0"},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return json_rpc_response(message["id"], {"tools": required_tools_payload()})
        if method == "tools/call":
            stream = oversized_initialization_stream(content_type)
            overflow_streams.append(stream)
            overflow_sent = True
            return httpx.Response(
                200,
                headers={
                    "content-type": content_type,
                    "mcp-session-id": "test-session",
                },
                stream=stream,
            )
        raise AssertionError(f"Unexpected MCP method: {method}")

    install_mock_mcp_transport(monkeypatch, mcp_handler)
    gateway = LangChainMcpGateway(settings)
    await replace_health_client(gateway)

    try:
        await gateway.initialize()
        ready_snapshot = gateway.contract_snapshot
        assert ready_snapshot.status == ContractStatus.READY
        started_at = perf_counter()

        with pytest.raises(McpResponseValidationError) as captured:
            async with asyncio.timeout(2.0):
                await gateway.answer_accessibility_question("Can I travel step-free?")

        assert perf_counter() - started_at < 1.5
        assert captured.value.context == {
            "reason": "mcp_response_body_limit",
            "tool": "answer_accessibility_question",
        }
        failed_snapshot = gateway.contract_snapshot
        assert failed_snapshot.status == ContractStatus.INCOMPATIBLE
        assert failed_snapshot.error_code == "mcp_response_body_limit"
        assert requested_methods.count("initialize") == 2
        assert "tools/list" in requested_methods
        assert "tools/call" in requested_methods
        assert overflow_streams[0].yielded_chunks == 2
        assert slow_delete_started is False
        assert slow_delete_completed is False
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_actual_mcp_sdk_caller_cancellation_aborts_before_slow_delete(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    tool_request_started = asyncio.Event()
    tool_handler_cancelled = asyncio.Event()
    delete_started_after_tool = asyncio.Event()

    async def mcp_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            if tool_request_started.is_set():
                delete_started_after_tool.set()
                await asyncio.sleep(2.0)
            return httpx.Response(200)

        message = json.loads(request.content)
        method = message["method"]
        if method == "initialize":
            return json_rpc_response(
                message["id"],
                {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "cancel-test-mcp", "version": "1.0.0"},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return json_rpc_response(message["id"], {"tools": required_tools_payload()})
        if method == "tools/call":
            tool_request_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                tool_handler_cancelled.set()
                raise
        raise AssertionError(f"Unexpected MCP method: {method}")

    install_mock_mcp_transport(monkeypatch, mcp_handler)
    gateway = LangChainMcpGateway(settings)
    await replace_health_client(gateway)

    try:
        await gateway.initialize()
        ready_snapshot = gateway.contract_snapshot
        assert ready_snapshot.status == ContractStatus.READY

        caller_task = asyncio.create_task(
            gateway.answer_accessibility_question("Can I travel step-free?")
        )
        async with asyncio.timeout(1.0):
            await tool_request_started.wait()
        cancelled_at = perf_counter()
        caller_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(1.0):
                await caller_task

        assert perf_counter() - cancelled_at < 1.0
        assert tool_handler_cancelled.is_set()
        assert not delete_started_after_tool.is_set()
        final_snapshot = gateway.contract_snapshot
        assert final_snapshot.status == ContractStatus.READY
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_actual_mcp_sdk_json_rpc_error_is_not_delayed_by_slow_delete(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    handler = SlowDeleteToolHandler(json_rpc_error=True)
    install_mock_mcp_transport(monkeypatch, handler)
    gateway = LangChainMcpGateway(settings)
    await replace_health_client(gateway)

    try:
        await gateway.initialize()
        ready_snapshot = gateway.contract_snapshot
        assert ready_snapshot.status == ContractStatus.READY

        with pytest.raises(McpUnavailableError):
            async with asyncio.timeout(4.0):
                await gateway.answer_accessibility_question("Can I travel step-free?")

        assert handler.delete_started.is_set()
        assert handler.delete_cancelled.is_set()
        assert handler.delete_completed is False
        failed_snapshot = gateway.contract_snapshot
        assert failed_snapshot.status == ContractStatus.UNAVAILABLE
        assert failed_snapshot.error_code == "MCP_UNAVAILABLE"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_actual_mcp_sdk_success_is_not_delayed_by_slow_delete(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    handler = SlowDeleteToolHandler(json_rpc_error=False)
    install_mock_mcp_transport(monkeypatch, handler)
    gateway = LangChainMcpGateway(settings)
    await replace_health_client(gateway)
    expected = trip_question_result()

    try:
        await gateway.initialize()
        ready_snapshot = gateway.contract_snapshot
        assert ready_snapshot.status == ContractStatus.READY

        async with asyncio.timeout(4.0):
            result = await gateway.answer_accessibility_question("Can I travel step-free?")

        assert result == expected
        assert handler.delete_started.is_set()
        assert handler.delete_cancelled.is_set()
        assert handler.delete_completed is False
        final_snapshot = gateway.contract_snapshot
        assert final_snapshot.status == ContractStatus.READY
    finally:
        await gateway.close()
