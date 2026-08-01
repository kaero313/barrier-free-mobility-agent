from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, Tool
from pydantic import BaseModel, ValidationError

from barrier_free_mobility_agent.config import McpAuthMode, Settings
from barrier_free_mobility_agent.domain.errors import (
    AgentError,
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
    McpContractSnapshot,
    MobilityProfile,
    QuestionIntent,
    StationResolutionResult,
)
from barrier_free_mobility_agent.integrations.mcp.auth import OidcClientCredentialsAuth
from barrier_free_mobility_agent.integrations.mcp.contract import (
    ANSWER_QUESTION_TOOL,
    GENERATE_ACCESSIBILITY_BRIEF_TOOL,
    RESOLVE_STATION_TOOL,
    fingerprint_tools,
    validate_health_payload,
    validate_required_tools,
)
from barrier_free_mobility_agent.integrations.mcp.payload_limits import (
    MCP_TOOL_SCHEMA_LIMITS,
    McpPayloadLimitViolation,
    McpResponseBodyLimitError,
    McpResponseLimitReason,
    enforce_mcp_response_body_limit,
    validate_mcp_payload_limits,
)

logger = logging.getLogger(__name__)

_SERVER_NAME = "mobility"
_HEALTH_RESPONSE_MAX_BYTES = 65_536
_TOOL_DISCOVERY_MAX_PAGES = 16
_TOOL_DISCOVERY_MAX_TOOLS = 64
_TOOL_DISCOVERY_MAX_BYTES = 1_048_576
_TOOL_CURSOR_MAX_CHARS = 1_000
_MCP_TRANSPORT_ABORT_TIMEOUT_SECONDS = 0.25
_MCP_SESSION_DELETE_TIMEOUT_SECONDS = 0.25


class _BoundedMcpAsyncClient(httpx.AsyncClient):
    """Keep best-effort MCP session termination outside the Tool request budget."""

    async def delete(
        self,
        url: httpx.URL | str,
        **kwargs: Any,
    ) -> httpx.Response:
        async with asyncio.timeout(_MCP_SESSION_DELETE_TIMEOUT_SECONDS):
            return await super().delete(url, **kwargs)


class _McpTransportViolationLatch:
    """Per-session, value-free signal for errors swallowed by the MCP SDK parser."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: McpResponseLimitReason | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def reason(self) -> McpResponseLimitReason | None:
        return self._reason

    def trip(self, reason: McpResponseLimitReason) -> None:
        if self._reason is None:
            self._reason = reason
            self._event.set()

    def bind_client(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def abort_client(self) -> None:
        client = self._client
        if client is None or client.is_closed:
            return
        try:
            async with asyncio.timeout(_MCP_TRANSPORT_ABORT_TIMEOUT_SECONDS):
                await client.aclose()
        except TimeoutError:
            # AsyncClient marks itself closed before awaiting transport cleanup. The session
            # teardown therefore cannot start a graceful DELETE after this bounded abort.
            return
        except Exception:
            # Cleanup failure must not replace the value-free policy violation.
            return

    async def wait(self) -> None:
        await self._event.wait()


_MCP_TRANSPORT_VIOLATION_LATCH: ContextVar[_McpTransportViolationLatch | None] = ContextVar(
    "mcp_transport_violation_latch",
    default=None,
)


class LangChainMcpGateway:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._oidc_auth = self._build_oidc_auth(settings)
        connection: StreamableHttpConnection = {
            "transport": "streamable_http",
            "url": settings.mcp_mobility_url,
            "headers": settings.mcp_headers,
            "timeout": timedelta(seconds=settings.mcp_connect_timeout_seconds),
            "sse_read_timeout": timedelta(seconds=settings.mcp_tool_timeout_seconds),
            "terminate_on_close": True,
            "httpx_client_factory": _bounded_mcp_http_client,
            "session_kwargs": {
                "read_timeout_seconds": timedelta(seconds=settings.mcp_tool_timeout_seconds)
            },
        }
        if self._oidc_auth is not None:
            connection["auth"] = self._oidc_auth
        self._client = MultiServerMCPClient(
            {_SERVER_NAME: connection},
            handle_tool_errors=False,
        )
        self._health_client = httpx.AsyncClient(
            auth=self._oidc_auth,
            headers=_identity_mcp_headers(settings.mcp_headers),
            timeout=httpx.Timeout(settings.mcp_connect_timeout_seconds),
        )
        self._initialize_lock = asyncio.Lock()
        self._contract_snapshot = McpContractSnapshot()

    @property
    def contract_snapshot(self) -> McpContractSnapshot:
        return self._contract_snapshot

    async def initialize(self) -> McpContractSnapshot:
        async with self._initialize_lock:
            checked_at = datetime.now(UTC)
            try:
                health_payload = await self._fetch_health()
                validate_health_payload(
                    health_payload,
                    expected_tool_input_max_chars=self._settings.mcp_tool_input_max_chars,
                    expected_auth_enabled=(
                        self._settings.mcp_mobility_auth_mode != McpAuthMode.NONE
                    ),
                )
                tools = await self._list_tools()
                capabilities = validate_required_tools(tools)
            except asyncio.CancelledError:
                raise
            except McpContractError as exc:
                self._contract_snapshot = McpContractSnapshot(
                    status=ContractStatus.INCOMPATIBLE,
                    checked_at=checked_at,
                    error_code=exc.context.get("reason", exc.code),
                )
                raise
            except McpDependencyDegradedError as exc:
                self._contract_snapshot = McpContractSnapshot(
                    status=ContractStatus.DEGRADED,
                    checked_at=checked_at,
                    error_code=exc.code,
                )
                raise
            except McpTimeoutError:
                self._contract_snapshot = McpContractSnapshot(
                    status=ContractStatus.UNAVAILABLE,
                    checked_at=checked_at,
                    error_code=McpTimeoutError.code,
                )
                raise
            except McpUnavailableError:
                self._contract_snapshot = McpContractSnapshot(
                    status=ContractStatus.UNAVAILABLE,
                    checked_at=checked_at,
                    error_code=McpUnavailableError.code,
                )
                raise
            except (TimeoutError, httpx.TimeoutException) as exc:
                timeout_error = McpTimeoutError(detail=type(exc).__name__)
                self._contract_snapshot = McpContractSnapshot(
                    status=ContractStatus.UNAVAILABLE,
                    checked_at=checked_at,
                    error_code=timeout_error.code,
                )
                raise timeout_error from exc
            except Exception as exc:
                if _contains_response_body_limit(exc):
                    contract_error = McpContractError(context={"reason": "mcp_response_body_limit"})
                    self._contract_snapshot = McpContractSnapshot(
                        status=ContractStatus.INCOMPATIBLE,
                        checked_at=checked_at,
                        error_code="mcp_response_body_limit",
                    )
                    raise contract_error from None
                if _contains_timeout(exc):
                    timeout_error = McpTimeoutError(detail="transport_timeout")
                    self._contract_snapshot = McpContractSnapshot(
                        status=ContractStatus.UNAVAILABLE,
                        checked_at=checked_at,
                        error_code=timeout_error.code,
                    )
                    raise timeout_error from None
                unavailable_error = McpUnavailableError(detail=type(exc).__name__)
                self._contract_snapshot = McpContractSnapshot(
                    status=ContractStatus.UNAVAILABLE,
                    checked_at=checked_at,
                    error_code=unavailable_error.code,
                )
                raise unavailable_error from exc

            self._contract_snapshot = McpContractSnapshot(
                status=ContractStatus.READY,
                checked_at=checked_at,
                tool_names=tuple(sorted(tool.name for tool in tools)),
                capabilities=capabilities,
                schema_fingerprint=fingerprint_tools(tools),
            )
            logger.info(
                "MCP contract ready: tools=%d fingerprint=%s",
                len(tools),
                self._contract_snapshot.schema_fingerprint,
            )
            return self._contract_snapshot

    async def refresh_if_stale(self) -> McpContractSnapshot:
        snapshot = self._contract_snapshot
        if snapshot.checked_at is not None:
            age = datetime.now(UTC) - snapshot.checked_at
            if age < timedelta(seconds=self._settings.mcp_contract_refresh_seconds):
                return snapshot
        return await self.initialize()

    async def answer_accessibility_question(
        self,
        question: str,
    ) -> AccessibilityQuestionResult:
        return await self._call_typed_tool(
            ANSWER_QUESTION_TOOL,
            {"question": question},
            AccessibilityQuestionResult,
        )

    async def answer_facility_question(
        self,
        question: str,
    ) -> AccessibilityQuestionResult:
        if McpCapability.FACILITY_QUESTION not in self._contract_snapshot.capabilities:
            raise McpContractError(context={"reason": "facility_capability_unavailable"})
        result = await self.answer_accessibility_question(question)
        if result.intent != QuestionIntent.FACILITY_STATUS:
            raise McpResponseValidationError(
                context={
                    "reason": "facility_intent_mismatch",
                    "actual_intent": result.intent.value,
                }
            )
        return result

    async def resolve_station(self, query: str) -> StationResolutionResult:
        return await self._call_typed_tool(
            RESOLVE_STATION_TOOL,
            {"query": query},
            StationResolutionResult,
        )

    async def generate_accessibility_brief(
        self,
        origin: str,
        destination: str,
        profile: MobilityProfile,
    ) -> AccessibilityResult:
        return await self._call_typed_tool(
            GENERATE_ACCESSIBILITY_BRIEF_TOOL,
            {
                "origin": origin,
                "destination": destination,
                "mobility_profile": profile.model_dump(mode="json"),
            },
            AccessibilityResult,
        )

    async def _call_typed_tool[ResultModel: BaseModel](
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result_model: type[ResultModel],
    ) -> ResultModel:
        snapshot = await self.refresh_if_stale()
        if snapshot.status == ContractStatus.INCOMPATIBLE:
            raise McpContractError(context={"reason": snapshot.error_code or "incompatible"})
        if snapshot.status == ContractStatus.DEGRADED:
            raise McpDependencyDegradedError()
        if snapshot.status != ContractStatus.READY:
            raise McpUnavailableError()

        async def call_tool(session: ClientSession) -> CallToolResult:
            return await session.call_tool(
                tool_name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=self._settings.mcp_tool_timeout_seconds),
            )

        try:
            result = await self._run_in_guarded_session(call_tool)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            self._mark_unavailable(McpTimeoutError.code)
            raise McpTimeoutError(detail=type(exc).__name__) from exc
        except AgentError:
            raise
        except Exception as exc:
            if _contains_response_body_limit(exc):
                self._mark_incompatible("mcp_response_body_limit")
                raise McpResponseValidationError(
                    context={
                        "reason": "mcp_response_body_limit",
                        "tool": tool_name,
                    }
                ) from None
            if _contains_timeout(exc):
                self._mark_unavailable(McpTimeoutError.code)
                raise McpTimeoutError(detail="transport_timeout") from None
            self._mark_unavailable(McpUnavailableError.code)
            raise McpUnavailableError(detail=type(exc).__name__) from exc

        if result.isError:
            raise McpToolExecutionError(context={"tool": tool_name})

        payload: Any = result.structuredContent
        if not isinstance(payload, dict):
            self._mark_incompatible("structured_content_missing")
            raise McpResponseValidationError(
                context={
                    "reason": "structured_content_missing",
                    "tool": tool_name,
                }
            )
        try:
            validate_mcp_payload_limits(payload)
        except McpPayloadLimitViolation as exc:
            self._mark_incompatible(f"structured_content_{exc.reason}_limit")
            raise McpResponseValidationError(
                context={
                    "reason": "structured_content_limit_exceeded",
                    "limit_kind": exc.reason,
                    "tool": tool_name,
                }
            ) from None
        try:
            return result_model.model_validate(payload)
        except ValidationError as exc:
            error_count = exc.error_count()
            self._mark_incompatible("structured_content_invalid")
            raise McpResponseValidationError(
                context={
                    "reason": "structured_content_invalid",
                    "error_count": error_count,
                    "tool": tool_name,
                },
            ) from None

    async def _run_in_guarded_session[Result](
        self,
        operation: Callable[[ClientSession], Awaitable[Result]],
    ) -> Result:
        """Run session initialization and its operation against a transport violation latch."""

        latch = _McpTransportViolationLatch()
        token = _MCP_TRANSPORT_VIOLATION_LATCH.set(latch)

        async def run_session() -> Result:
            async with self._client.session(_SERVER_NAME) as session:
                return await operation(session)

        operation_task = asyncio.create_task(run_session())
        violation_task = asyncio.create_task(latch.wait())
        try:
            await asyncio.wait(
                {operation_task, violation_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            reason = latch.reason
            if reason is not None:
                await latch.abort_client()
                raise McpResponseBodyLimitError(reason)
            return await operation_task
        finally:
            if not operation_task.done():
                await latch.abort_client()
            for task in (operation_task, violation_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation_task, violation_task, return_exceptions=True)
            _MCP_TRANSPORT_VIOLATION_LATCH.reset(token)

    async def close(self) -> None:
        await self._health_client.aclose()
        if self._oidc_auth is not None:
            await self._oidc_auth.aclose()

    @staticmethod
    def _build_oidc_auth(settings: Settings) -> OidcClientCredentialsAuth | None:
        if settings.mcp_mobility_auth_mode != McpAuthMode.OIDC:
            return None
        token_url = settings.mcp_mobility_oidc_token_url
        client_id = settings.mcp_mobility_oidc_client_id
        client_secret = settings.mcp_mobility_oidc_client_secret
        if token_url is None or client_id is None or client_secret is None:
            raise ValueError("Validated OIDC settings are incomplete.")
        return OidcClientCredentialsAuth(
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=settings.mcp_mobility_oidc_scope,
            timeout_seconds=settings.mcp_connect_timeout_seconds,
            refresh_leeway_seconds=settings.mcp_mobility_oidc_refresh_leeway_seconds,
        )

    async def _fetch_health(self) -> dict[str, Any]:
        try:
            async with self._health_client.stream(
                "GET",
                self._settings.mcp_mobility_health_url,
            ) as response:
                response.raise_for_status()
                raw_payload = await _read_bounded_response(
                    response,
                    limit=_HEALTH_RESPONSE_MAX_BYTES,
                )
        except httpx.TimeoutException as exc:
            raise McpTimeoutError(detail=type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise McpUnavailableError(detail=type(exc).__name__) from exc
        try:
            payload = json.loads(raw_payload)
        except ValueError:
            raise McpContractError(
                detail="MCP health response is not JSON.",
                context={"reason": "health_response_not_json"},
            ) from None
        if not isinstance(payload, dict):
            raise McpContractError(
                detail="MCP health response must be an object.",
                context={"reason": "health_response_not_object"},
            )
        try:
            validate_mcp_payload_limits(payload)
        except McpPayloadLimitViolation as exc:
            raise McpContractError(
                context={"reason": f"health_payload_{exc.reason}_limit"}
            ) from None
        return payload

    async def _list_tools(self) -> list[Tool]:
        async def discover_tools(
            session: ClientSession,
        ) -> tuple[list[Tool], McpContractError | None]:
            tools: list[Tool] = []
            cursor: str | None = None
            seen_cursors: set[str] = set()
            seen_tool_names: set[str] = set()
            discovery_bytes = 0
            page_count = 0
            validation_failure: McpContractError | None = None
            while True:
                page_count += 1
                if page_count > _TOOL_DISCOVERY_MAX_PAGES:
                    validation_failure = McpContractError(
                        context={"reason": "tool_page_limit_exceeded"}
                    )
                    break
                page = await session.list_tools(cursor=cursor)
                try:
                    if len(tools) + len(page.tools) > _TOOL_DISCOVERY_MAX_TOOLS:
                        raise McpContractError(context={"reason": "tool_count_limit_exceeded"})
                    for tool in page.tools:
                        if tool.name in seen_tool_names:
                            raise McpContractError(context={"reason": "tool_name_duplicate"})
                        seen_tool_names.add(tool.name)
                        discovery_bytes += validate_mcp_payload_limits(
                            tool.model_dump(mode="json"),
                            limits=MCP_TOOL_SCHEMA_LIMITS,
                        )
                        if discovery_bytes > _TOOL_DISCOVERY_MAX_BYTES:
                            raise McpContractError(
                                context={"reason": "tool_discovery_bytes_limit_exceeded"}
                            )
                    tools.extend(page.tools)
                    next_cursor = page.nextCursor
                    if next_cursor is None:
                        break
                    if not isinstance(next_cursor, str) or not (
                        1 <= len(next_cursor) <= _TOOL_CURSOR_MAX_CHARS
                    ):
                        raise McpContractError(context={"reason": "tool_cursor_invalid"})
                    if next_cursor in seen_cursors:
                        raise McpContractError(context={"reason": "tool_cursor_cycle"})
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                except McpPayloadLimitViolation as exc:
                    validation_failure = McpContractError(
                        context={"reason": f"tool_schema_{exc.reason}_limit"}
                    )
                    break
                except McpContractError as exc:
                    validation_failure = exc
                    break
            return tools, validation_failure

        tools, validation_failure = await self._run_in_guarded_session(discover_tools)
        if validation_failure is not None:
            raise validation_failure
        return tools

    def _mark_unavailable(self, error_code: str) -> None:
        self._contract_snapshot = McpContractSnapshot(
            status=ContractStatus.UNAVAILABLE,
            checked_at=datetime.now(UTC),
            tool_names=self._contract_snapshot.tool_names,
            capabilities=self._contract_snapshot.capabilities,
            schema_fingerprint=self._contract_snapshot.schema_fingerprint,
            error_code=error_code,
        )

    def _mark_incompatible(self, error_code: str) -> None:
        self._contract_snapshot = McpContractSnapshot(
            status=ContractStatus.INCOMPATIBLE,
            checked_at=datetime.now(UTC),
            tool_names=self._contract_snapshot.tool_names,
            capabilities=self._contract_snapshot.capabilities,
            schema_fingerprint=self._contract_snapshot.schema_fingerprint,
            error_code=error_code,
        )


async def _read_bounded_response(response: httpx.Response, *, limit: int) -> bytes:
    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise McpContractError(context={"reason": "health_response_compression_unsupported"})
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise McpContractError(context={"reason": "health_content_length_invalid"}) from None
        if declared_length < 0:
            raise McpContractError(context={"reason": "health_content_length_invalid"})
        if declared_length > limit:
            raise McpContractError(context={"reason": "health_response_bytes_limit"})

    payload = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=16_384):
        if len(payload) + len(chunk) > limit:
            raise McpContractError(context={"reason": "health_response_bytes_limit"})
        payload.extend(chunk)
    return bytes(payload)


def _bounded_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    latch = _MCP_TRANSPORT_VIOLATION_LATCH.get()
    response_hooks: list[Callable[[httpx.Response], Awaitable[None]]]
    if latch is None:
        response_hooks = [enforce_mcp_response_body_limit]
    else:
        active_latch = latch

        async def guarded_response_hook(response: httpx.Response) -> None:
            await enforce_mcp_response_body_limit(
                response,
                on_violation=active_latch.trip,
            )

        response_hooks = [guarded_response_hook]

    client = _BoundedMcpAsyncClient(
        headers=_identity_mcp_headers(headers),
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        auth=auth,
        follow_redirects=True,
        event_hooks={"response": response_hooks},
        transport=transport,
    )
    if latch is not None:
        latch.bind_client(client)
    return client


def _identity_mcp_headers(headers: dict[str, str] | None) -> dict[str, str]:
    identity_headers = {
        name: value for name, value in (headers or {}).items() if name.lower() != "accept-encoding"
    }
    identity_headers["Accept-Encoding"] = "identity"
    return identity_headers


def _contains_timeout(exc: BaseException) -> bool:
    return any(
        isinstance(leaf, TimeoutError | httpx.TimeoutException)
        or (isinstance(leaf, McpError) and int(leaf.error.code) == 408)
        for leaf in _exception_leaves(exc)
    )


def _contains_response_body_limit(exc: BaseException) -> bool:
    return any(isinstance(leaf, McpResponseBodyLimitError) for leaf in _exception_leaves(exc))


def _exception_leaves(exc: BaseException) -> Iterator[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            yield from _exception_leaves(nested)
        return
    yield exc
