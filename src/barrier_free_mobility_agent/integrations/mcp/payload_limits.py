from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

MCP_PAYLOAD_MAX_UTF8_BYTES = 262_144
MCP_PAYLOAD_MAX_STRING_CHARS = 10_000
MCP_PAYLOAD_MAX_ARRAY_ITEMS = 100
MCP_PAYLOAD_MAX_OBJECT_FIELDS = 100
MCP_PAYLOAD_MAX_DEPTH = 16
MCP_PAYLOAD_MAX_NODES = 4_096
MCP_HTTP_RESPONSE_MAX_BYTES = 1_048_576

type PayloadLimitReason = Literal[
    "array_items",
    "depth",
    "invalid_json_value",
    "nodes",
    "object_fields",
    "string_chars",
    "utf8_bytes",
]
type McpResponseLimitReason = Literal[
    "async_stream_invalid",
    "body_bytes",
    "compression",
    "content_length_invalid",
    "sse_event_bytes",
]
type McpResponseViolationCallback = Callable[[McpResponseLimitReason], None]


@dataclass(frozen=True, slots=True)
class McpPayloadLimitViolation(ValueError):
    """A value-free description of an untrusted MCP payload limit violation."""

    reason: PayloadLimitReason
    limit: int

    def __str__(self) -> str:
        return f"MCP payload exceeds {self.reason} limit ({self.limit})."


class McpResponseBodyLimitError(httpx.StreamError):
    """An MCP HTTP response violated the pre-decode transport body policy."""

    def __init__(self, reason: McpResponseLimitReason) -> None:
        super().__init__("MCP response violated the transport body policy.")
        self.reason = reason

    def __str__(self) -> str:
        if self.reason == "compression":
            return "Compressed MCP responses are not accepted."
        if self.reason == "content_length_invalid":
            return "MCP response Content-Length is invalid."
        if self.reason == "sse_event_bytes":
            return "MCP SSE event exceeds its byte limit."
        if self.reason == "async_stream_invalid":
            return "MCP async response stream is invalid."
        return "MCP response body exceeds its byte limit."


@dataclass(frozen=True, slots=True)
class McpPayloadLimits:
    utf8_bytes: int
    string_chars: int
    array_items: int
    object_fields: int
    depth: int
    nodes: int


MCP_RESULT_PAYLOAD_LIMITS = McpPayloadLimits(
    utf8_bytes=MCP_PAYLOAD_MAX_UTF8_BYTES,
    string_chars=MCP_PAYLOAD_MAX_STRING_CHARS,
    array_items=MCP_PAYLOAD_MAX_ARRAY_ITEMS,
    object_fields=MCP_PAYLOAD_MAX_OBJECT_FIELDS,
    depth=MCP_PAYLOAD_MAX_DEPTH,
    nodes=MCP_PAYLOAD_MAX_NODES,
)
MCP_TOOL_SCHEMA_LIMITS = McpPayloadLimits(
    utf8_bytes=MCP_PAYLOAD_MAX_UTF8_BYTES,
    string_chars=MCP_PAYLOAD_MAX_STRING_CHARS,
    array_items=MCP_PAYLOAD_MAX_ARRAY_ITEMS,
    object_fields=MCP_PAYLOAD_MAX_OBJECT_FIELDS,
    depth=32,
    nodes=16_384,
)


class BoundedMcpResponseStream(httpx.AsyncByteStream):
    """Stop a chunked MCP POST response before the protocol parser buffers it."""

    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        *,
        limit: int,
        on_violation: McpResponseViolationCallback | None = None,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._on_violation = on_violation

    async def __aiter__(self) -> AsyncIterator[bytes]:
        consumed = 0
        async for chunk in self._stream:
            consumed += len(chunk)
            if consumed > self._limit:
                raise _response_limit_error("body_bytes", self._on_violation)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class BoundedMcpEventStream(httpx.AsyncByteStream):
    """Bound each decoded SSE event while allowing a long-lived sequence of small events."""

    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        *,
        event_limit: int,
        on_violation: McpResponseViolationCallback | None = None,
    ) -> None:
        self._stream = stream
        self._event_limit = event_limit
        self._on_violation = on_violation
        self._event_bytes = 0
        self._line_has_content = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            if b"\n" not in chunk:
                self._event_bytes += len(chunk)
                if not self._line_has_content and chunk:
                    self._line_has_content = chunk[0] != 0x0D or bool(chunk.lstrip(b"\r"))
                if self._event_bytes > self._event_limit:
                    raise _response_limit_error("sse_event_bytes", self._on_violation)
                yield chunk
                continue
            for byte in chunk:
                self._event_bytes += 1
                if byte == 0x0A:
                    if not self._line_has_content:
                        self._event_bytes = 0
                    self._line_has_content = False
                elif byte != 0x0D:
                    self._line_has_content = True
                if self._event_bytes > self._event_limit:
                    raise _response_limit_error("sse_event_bytes", self._on_violation)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


async def enforce_mcp_response_body_limit(
    response: httpx.Response,
    *,
    on_violation: McpResponseViolationCallback | None = None,
) -> None:
    """HTTPX response hook for MCP response bodies.

    Compression is rejected so the transport count is also the decoded count. Long-lived SSE
    streams are bounded per event; other bodies use one total response limit.
    """

    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise _response_limit_error("compression", on_violation)
    content_type = (
        response.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
    )
    is_event_stream = content_type == "text/event-stream"
    content_length = response.headers.get("content-length")
    if content_length is not None and not is_event_stream:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise _response_limit_error("content_length_invalid", on_violation) from None
        if declared_length < 0:
            raise _response_limit_error("content_length_invalid", on_violation)
        if declared_length > MCP_HTTP_RESPONSE_MAX_BYTES:
            raise _response_limit_error("body_bytes", on_violation)
    stream = response.stream
    if not isinstance(stream, httpx.AsyncByteStream):
        raise _response_limit_error("async_stream_invalid", on_violation)
    response.stream = (
        BoundedMcpEventStream(
            stream,
            event_limit=MCP_HTTP_RESPONSE_MAX_BYTES,
            on_violation=on_violation,
        )
        if is_event_stream
        else BoundedMcpResponseStream(
            stream,
            limit=MCP_HTTP_RESPONSE_MAX_BYTES,
            on_violation=on_violation,
        )
    )


def _response_limit_error(
    reason: McpResponseLimitReason,
    on_violation: McpResponseViolationCallback | None,
) -> McpResponseBodyLimitError:
    if on_violation is not None:
        on_violation(reason)
    return McpResponseBodyLimitError(reason)


def validate_mcp_payload_limits(
    payload: dict[str, Any],
    *,
    limits: McpPayloadLimits = MCP_RESULT_PAYLOAD_LIMITS,
) -> int:
    """Reject structurally valid but operationally unsafe MCP result payloads.

    The check deliberately reports no path or value so an external location, question, or
    provider response cannot leak into logs and error details.
    """

    remaining_nodes = limits.nodes

    def visit(value: Any, *, depth: int) -> None:
        nonlocal remaining_nodes
        remaining_nodes -= 1
        if remaining_nodes < 0:
            raise McpPayloadLimitViolation("nodes", limits.nodes)
        if depth > limits.depth:
            raise McpPayloadLimitViolation("depth", limits.depth)
        if value is None or isinstance(value, bool | int | float):
            return
        if isinstance(value, str):
            if len(value) > limits.string_chars:
                raise McpPayloadLimitViolation(
                    "string_chars",
                    limits.string_chars,
                )
            return
        if isinstance(value, list):
            if len(value) > limits.array_items:
                raise McpPayloadLimitViolation(
                    "array_items",
                    limits.array_items,
                )
            for item in value:
                visit(item, depth=depth + 1)
            return
        if isinstance(value, dict):
            if len(value) > limits.object_fields:
                raise McpPayloadLimitViolation(
                    "object_fields",
                    limits.object_fields,
                )
            for key, item in value.items():
                if not isinstance(key, str):
                    raise McpPayloadLimitViolation("invalid_json_value", 0)
                visit(key, depth=depth + 1)
                visit(item, depth=depth + 1)
            return
        raise McpPayloadLimitViolation("invalid_json_value", 0)

    visit(payload, depth=0)
    size = 0
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        for chunk in encoder.iterencode(payload):
            size += len(chunk.encode("utf-8"))
            if size > limits.utf8_bytes:
                raise McpPayloadLimitViolation("utf8_bytes", limits.utf8_bytes)
    except McpPayloadLimitViolation:
        raise
    except (TypeError, ValueError):
        raise McpPayloadLimitViolation("invalid_json_value", 0) from None
    return size
