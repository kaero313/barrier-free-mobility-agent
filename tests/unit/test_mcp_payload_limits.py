from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from barrier_free_mobility_agent.integrations.mcp.payload_limits import (
    MCP_HTTP_RESPONSE_MAX_BYTES,
    MCP_PAYLOAD_MAX_ARRAY_ITEMS,
    MCP_PAYLOAD_MAX_DEPTH,
    MCP_PAYLOAD_MAX_OBJECT_FIELDS,
    MCP_PAYLOAD_MAX_STRING_CHARS,
    MCP_PAYLOAD_MAX_UTF8_BYTES,
    BoundedMcpEventStream,
    BoundedMcpResponseStream,
    McpPayloadLimitViolation,
    McpResponseBodyLimitError,
    enforce_mcp_response_body_limit,
    validate_mcp_payload_limits,
)


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def test_mcp_payload_limits_accept_bounded_korean_and_emoji_json() -> None:
    payload = {
        "message": "서울역에서 시청역까지 이동할 수 있나요? 🧑‍🦽",
        "items": [{"status": "UNKNOWN"}],
    }

    size = validate_mcp_payload_limits(payload)

    assert size == len(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"value": "가" * (MCP_PAYLOAD_MAX_STRING_CHARS + 1)}, "string_chars"),
        ({"items": [None] * (MCP_PAYLOAD_MAX_ARRAY_ITEMS + 1)}, "array_items"),
        (
            {f"field-{index}": None for index in range(MCP_PAYLOAD_MAX_OBJECT_FIELDS + 1)},
            "object_fields",
        ),
        (
            {
                "groups": [
                    [None] * MCP_PAYLOAD_MAX_ARRAY_ITEMS for _ in range(MCP_PAYLOAD_MAX_ARRAY_ITEMS)
                ]
            },
            "nodes",
        ),
    ],
)
def test_mcp_payload_limits_reject_oversized_values(
    payload: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(McpPayloadLimitViolation) as exc_info:
        validate_mcp_payload_limits(payload)

    assert exc_info.value.reason == reason
    assert "가" not in str(exc_info.value)


def test_mcp_payload_limits_reject_excessive_depth() -> None:
    payload: dict[str, object] = {}
    current = payload
    for _ in range(MCP_PAYLOAD_MAX_DEPTH + 1):
        nested: dict[str, object] = {}
        current["child"] = nested
        current = nested

    with pytest.raises(McpPayloadLimitViolation) as exc_info:
        validate_mcp_payload_limits(payload)

    assert exc_info.value.reason == "depth"


def test_mcp_payload_limits_reject_total_canonical_utf8_bytes() -> None:
    chunk = "가" * min(MCP_PAYLOAD_MAX_STRING_CHARS, 9_000)
    payload = {
        "items": [chunk for _ in range(MCP_PAYLOAD_MAX_ARRAY_ITEMS)],
    }

    with pytest.raises(McpPayloadLimitViolation) as exc_info:
        validate_mcp_payload_limits(payload)

    assert exc_info.value.reason == "utf8_bytes"
    assert MCP_PAYLOAD_MAX_UTF8_BYTES > MCP_PAYLOAD_MAX_STRING_CHARS


@pytest.mark.parametrize(
    "payload",
    [
        {"value": object()},
        {"value": float("nan")},
    ],
)
def test_mcp_payload_limits_reject_non_json_values(payload: dict[str, object]) -> None:
    with pytest.raises(McpPayloadLimitViolation) as exc_info:
        validate_mcp_payload_limits(payload)

    assert exc_info.value.reason == "invalid_json_value"


def test_mcp_payload_limits_reject_non_string_object_keys() -> None:
    payload: dict[object, object] = {1: "value"}

    with pytest.raises(McpPayloadLimitViolation) as exc_info:
        validate_mcp_payload_limits(payload)  # type: ignore[arg-type]

    assert exc_info.value.reason == "invalid_json_value"


@pytest.mark.asyncio
async def test_bounded_mcp_stream_stops_chunked_body_before_forwarding_overflow() -> None:
    source = _ChunkStream(b"ab", b"cd")
    bounded = BoundedMcpResponseStream(source, limit=3)
    received: list[bytes] = []

    async def consume() -> None:
        async for chunk in bounded:
            received.append(chunk)

    with pytest.raises(httpx.StreamError, match="byte limit"):
        await consume()

    assert received == [b"ab"]
    await bounded.aclose()
    assert source.closed is True


@pytest.mark.asyncio
async def test_mcp_response_hook_rejects_declared_oversize_and_bounds_get_stream() -> None:
    post = httpx.Response(
        200,
        request=httpx.Request("POST", "https://mcp.example/mcp"),
        headers={"content-length": str(MCP_HTTP_RESPONSE_MAX_BYTES + 1)},
        stream=_ChunkStream(b"ignored"),
    )
    with pytest.raises(httpx.StreamError, match="byte limit"):
        await enforce_mcp_response_body_limit(post)

    get_stream = _ChunkStream(b"response")
    get = httpx.Response(
        200,
        request=httpx.Request("GET", "https://mcp.example/mcp"),
        stream=get_stream,
    )
    await enforce_mcp_response_body_limit(get)
    assert isinstance(get.stream, BoundedMcpResponseStream)


@pytest.mark.asyncio
async def test_mcp_response_hook_rejects_compression_before_reading() -> None:
    source = _ChunkStream(b"compressed")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://mcp.example/mcp"),
        headers={"content-encoding": "gzip"},
        stream=source,
    )

    with pytest.raises(McpResponseBodyLimitError, match="Compressed"):
        await enforce_mcp_response_body_limit(response)

    assert response.stream is source
    assert source.closed is False


@pytest.mark.asyncio
async def test_mcp_response_hook_bounds_each_sse_event_independently() -> None:
    source = _ChunkStream(b"data: 1\n\ndata: 2\n\n")
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://mcp.example/mcp"),
        headers={"content-type": "text/event-stream; charset=utf-8"},
        stream=source,
    )

    await enforce_mcp_response_body_limit(response)

    assert isinstance(response.stream, BoundedMcpEventStream)


@pytest.mark.asyncio
async def test_bounded_mcp_sse_stream_allows_many_events_but_rejects_one_large_event() -> None:
    source = _ChunkStream(b"data: 1\n\ndata: 2\n\n", b"data: too-large\n\n")
    bounded = BoundedMcpEventStream(source, event_limit=12)
    received: list[bytes] = []

    async def consume() -> None:
        async for chunk in bounded:
            received.append(chunk)

    with pytest.raises(McpResponseBodyLimitError, match="SSE event"):
        await consume()

    assert received == [b"data: 1\n\ndata: 2\n\n"]


@pytest.mark.asyncio
async def test_mcp_response_hook_wraps_post_stream_without_reading_it() -> None:
    source = _ChunkStream(b"response")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://mcp.example/mcp"),
        stream=source,
    )

    await enforce_mcp_response_body_limit(response)

    assert isinstance(response.stream, BoundedMcpResponseStream)
    assert source.closed is False
