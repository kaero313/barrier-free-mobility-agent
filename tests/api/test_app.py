from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import Error as PsycopgError
from psycopg_pool import PoolClosed, PoolTimeout
from tests.factories import (
    CANONICAL_MESSAGE,
    QUESTION,
    clarification_result,
    facility_question_result,
    trip_question_result,
)
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.api import app as app_module
from barrier_free_mobility_agent.api.app import create_app
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.errors import (
    McpResponseValidationError,
    McpTimeoutError,
    McpToolExecutionError,
    McpUnavailableError,
)
from barrier_free_mobility_agent.domain.models import (
    ContractStatus,
    McpContractSnapshot,
    ResponseStatus,
    RiskLevel,
)


@asynccontextmanager
async def api_client(
    app: FastAPI,
    *,
    raise_app_exceptions: bool = True,
) -> AsyncIterator[AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(
                app=app,
                raise_app_exceptions=raise_app_exceptions,
            ),
            base_url="http://testserver",
        ) as client,
    ):
        yield client


@pytest.mark.asyncio
async def test_liveness_and_readiness_are_separate(settings: Settings) -> None:
    gateway = FakeMcpGateway(
        snapshot=McpContractSnapshot(
            status=ContractStatus.UNAVAILABLE,
            error_code="MCP_UNAVAILABLE",
        ),
        initialize_error=McpUnavailableError(),
    )
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert set(ready.json()["dependencies"]) == {"mobility_mcp", "persistence"}
    assert ready.json()["dependencies"]["mobility_mcp"]["status"] == "unavailable"
    assert ready.json()["dependencies"]["persistence"] == {
        "status": "ready",
        "backend": "memory",
    }
    assert "tool_names" not in ready.json()["dependencies"]["mobility_mcp"]


@pytest.mark.asyncio
async def test_degraded_dependency_is_not_ready(settings: Settings) -> None:
    gateway = FakeMcpGateway(
        snapshot=McpContractSnapshot(
            status=ContractStatus.DEGRADED,
            error_code="MCP_DEGRADED",
        )
    )
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        ready = await client.get("/health/ready")

    assert ready.status_code == 503
    assert ready.json()["dependencies"]["mobility_mcp"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_unavailable_persistence_makes_the_product_not_ready(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        monkeypatch.setattr(
            app.state.container.conversations,
            "is_ready",
            AsyncMock(return_value=False),
        )
        ready = await client.get("/health/ready")

    assert ready.status_code == 503
    assert ready.json()["dependencies"]["persistence"] == {
        "status": "unavailable",
        "backend": "memory",
    }


@pytest.mark.asyncio
async def test_fail_fast_startup_still_closes_gateway(settings: Settings) -> None:
    fail_fast = settings.model_copy(update={"mcp_startup_required": True})
    gateway = FakeMcpGateway(initialize_error=McpUnavailableError())
    app = create_app(settings=fail_fast, mcp_gateway=gateway)

    with pytest.raises(McpUnavailableError):
        async with app.router.lifespan_context(app):
            pass

    assert gateway.closed is True


@pytest.mark.asyncio
async def test_persistence_startup_failure_does_not_fall_back_to_memory(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def failing_persistence(active_settings: Settings) -> AsyncIterator[None]:
        if active_settings.app_name:
            raise RuntimeError("database unavailable")
        yield None

    gateway = FakeMcpGateway()
    monkeypatch.setattr(app_module, "open_persistence", failing_persistence)
    app = create_app(settings=settings, mcp_gateway=gateway)

    with pytest.raises(RuntimeError, match="database unavailable"):
        async with app.router.lifespan_context(app):
            pass

    assert gateway.initialize_calls == 0
    assert gateway.closed is True


@pytest.mark.asyncio
async def test_readiness_recovers_without_restarting_app(settings: Settings) -> None:
    gateway = FakeMcpGateway(
        snapshot=McpContractSnapshot(
            status=ContractStatus.UNAVAILABLE,
            error_code="MCP_UNAVAILABLE",
        ),
        initialize_error=McpUnavailableError(),
    )
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        assert (await client.get("/health/ready")).status_code == 503
        gateway.initialize_error = None
        gateway.set_snapshot(
            McpContractSnapshot(
                status=ContractStatus.READY,
                tool_names=("answer_accessibility_question",),
                schema_fingerprint="recovered",
            )
        )
        recovered = await client.get("/health/ready")

    assert recovered.status_code == 200
    assert recovered.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_question_returns_agent_owned_projection(settings: Settings) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
            headers={"X-Request-ID": "request-123"},
        )

    body = response.json()
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "request-123"
    assert body["schema_version"] == "3"
    assert body["request_id"] == "request-123"
    assert body["source"] == "mcp"
    assert body["user_message"] == CANONICAL_MESSAGE
    assert body["evidence"]["risk_level"] == "CAUTION"
    assert body["presentation"]["kind"] == "trip"
    assert body["presentation"]["trip"] == {
        "origin": "홍대입구",
        "destination": "삼성",
        "route_summary": "홍대입구에서 삼성까지 1회 환승",
    }
    assert body["presentation"]["safety_notice"] == "출발 직전 재확인하세요."
    assert body["clarification"] is None
    assert "data" not in body
    assert "mobility_profile" not in body
    assert gateway.answer_calls == []
    assert gateway.resolve_calls == ["홍대입구", "삼성"]
    assert len(gateway.brief_calls) == 1


@pytest.mark.asyncio
async def test_partial_unknown_result_is_not_upgraded(settings: Settings) -> None:
    gateway = FakeMcpGateway(
        result=trip_question_result(
            status=ResponseStatus.PARTIAL,
            risk_level=RiskLevel.UNKNOWN,
        )
    )
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "PARTIAL"
    assert body["evidence"]["risk_level"] == "UNKNOWN"
    assert body["evidence"]["failed_sources"] == [
        {
            "source_name": "facility_information",
            "display_name": "역 시설 정보",
            "recoverable": True,
        }
    ]
    assert "elevator_status" not in response.text
    assert "temporary failure" not in response.text
    assert body["evidence"]["limitations"]


@pytest.mark.asyncio
async def test_empty_facility_items_do_not_become_an_absence_claim(
    settings: Settings,
) -> None:
    canonical_message = "시설 조회 결과만으로는 설치 여부를 확인할 수 없습니다."
    result = facility_question_result()
    assert result.facility_result is not None
    facility_result = result.facility_result.model_copy(
        update={"items": [], "user_message": canonical_message}
    )
    gateway = FakeMcpGateway(
        result=result.model_copy(
            update={
                "facility_result": facility_result,
                "user_message": canonical_message,
            }
        )
    )
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": "강남역 엘리베이터가 있어?"},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["source"] == "mcp"
    assert body["user_message"] == canonical_message
    assert "시설이 없습니다" not in body["user_message"]


@pytest.mark.asyncio
async def test_failed_and_clarification_business_outcomes_remain_http_200(
    settings: Settings,
) -> None:
    failed_gateway = FakeMcpGateway(
        result=trip_question_result(
            status=ResponseStatus.FAILED,
            risk_level=RiskLevel.UNKNOWN,
        )
    )
    clarification_gateway = FakeMcpGateway(result=clarification_result())

    async with api_client(create_app(settings=settings, mcp_gateway=failed_gateway)) as client:
        failed = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
        )
    async with api_client(
        create_app(settings=settings, mcp_gateway=clarification_gateway)
    ) as client:
        clarification = await client.post(
            "/v1/accessibility/questions",
            json={"question": "휠체어로 갈 수 있어?"},
        )

    assert failed.status_code == 200
    assert failed.json()["status"] == "FAILED"
    assert clarification.status_code == 200
    assert clarification.json()["status"] == "NEEDS_CLARIFICATION"
    assert clarification.json()["questions"]
    assert clarification.json()["clarification"]["control"] == "free_text"
    assert [prompt["fields"] for prompt in clarification.json()["clarification"]["prompts"]] == [
        ["origin"],
        ["destination"],
        ["can_use_stairs", "can_use_escalator"],
    ]


@pytest.mark.asyncio
async def test_long_mcp_input_returns_clarification_without_calling_mcp(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": "가" * 121},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "NEEDS_CLARIFICATION"
    assert body["source"] == "agent"
    assert body["clarification_needed"] is True
    assert gateway.answer_calls == []


@pytest.mark.asyncio
async def test_long_structured_trip_uses_compact_graph_tool_inputs(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)
    question = ("추가 설명 " * 20) + QUESTION
    assert len(question) > settings.mcp_tool_input_max_chars

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": question},
        )

    assert response.status_code == 200
    assert response.json()["source"] == "mcp"
    assert gateway.answer_calls == []
    assert gateway.resolve_calls == ["홍대입구", "삼성"]
    assert len(gateway.brief_calls) == 1


@pytest.mark.asyncio
async def test_input_over_api_limit_returns_422(settings: Settings) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": "가" * 2001},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["schema_version"] == "2"
    assert body["error"]["code"] == "QUESTION_TOO_LONG"
    assert body["error"]["detail"] == {}
    assert gateway.answer_calls == []


@pytest.mark.asyncio
async def test_unknown_request_field_uses_standard_error_envelope(
    settings: Settings,
) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION, "unexpected": True},
            headers={"X-Request-ID": "validation-1"},
        )

    body = response.json()
    assert response.status_code == 422
    assert body["request_id"] == "validation-1"
    assert response.headers["X-Request-ID"] == body["request_id"]
    assert body["schema_version"] == "2"
    assert body["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert "input" not in body["error"]["detail"]["issues"][0]


@pytest.mark.asyncio
async def test_blank_question_uses_standard_error_envelope(settings: Settings) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": "   "},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_QUESTION"


@pytest.mark.asyncio
async def test_invalid_request_id_is_replaced(settings: Settings) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
            headers={"X-Request-ID": "bad request id"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "bad request id"
    assert len(response.headers["X-Request-ID"]) == 32


@pytest.mark.asyncio
async def test_mcp_timeout_maps_to_504(settings: Settings) -> None:
    gateway = FakeMcpGateway(brief_error=McpTimeoutError())
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
        )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "MCP_TIMEOUT"


@pytest.mark.asyncio
async def test_mcp_tool_error_maps_to_502(settings: Settings) -> None:
    gateway = FakeMcpGateway(brief_error=McpToolExecutionError())
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "MCP_TOOL_EXECUTION_ERROR"
    assert response.json()["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_malformed_mcp_response_maps_to_502(settings: Settings) -> None:
    gateway = FakeMcpGateway(
        brief_error=McpResponseValidationError(
            detail="sensitive provider exception",
            context={"tool": "private-tool", "reason": "private-reason"},
        )
    )
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
        )

    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "MCP_RESPONSE_VALIDATION_ERROR",
        "message": "이동 정보 응답을 안전하게 검증하지 못했습니다.",
        "retryable": False,
        "detail": {},
    }
    assert "sensitive" not in response.text
    assert "private-tool" not in response.text


@pytest.mark.asyncio
async def test_unregistered_agent_error_fails_closed_at_the_api_boundary(
    settings: Settings,
) -> None:
    class PrivateTimeoutError(McpTimeoutError):
        code = "PRIVATE_TIMEOUT"
        public_message = "sensitive private message"
        retryable = True

    gateway = FakeMcpGateway(
        answer_error=PrivateTimeoutError(
            detail="sensitive exception",
            context={"secret": "sensitive context"},
        )
    )
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": "오늘 날씨를 알려줘"},
            headers={"X-Request-ID": "unknown-agent-error"},
        )

    assert response.status_code == 500
    assert response.json() == {
        "schema_version": "2",
        "request_id": "unknown-agent-error",
        "error": {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "요청을 처리하는 중 예기치 않은 오류가 발생했습니다.",
            "retryable": False,
            "detail": {},
        },
    }
    assert "PRIVATE_TIMEOUT" not in response.text
    assert "sensitive" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            PsycopgError("sensitive database detail with private question"),
            id="psycopg-error",
        ),
        pytest.param(
            PoolTimeout("sensitive database detail with private question"),
            id="pool-timeout",
        ),
        pytest.param(
            PoolClosed("sensitive database detail with private question"),
            id="pool-closed",
        ),
    ],
)
async def test_persistence_errors_use_retryable_safe_503_on_stateful_mutation(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())
    warning = Mock()
    monkeypatch.setattr(app_module.logger, "warning", warning)

    async with api_client(app, raise_app_exceptions=False) as client:
        monkeypatch.setattr(
            app.state.container.conversations,
            "create_thread",
            AsyncMock(side_effect=failure),
        )
        response = await client.post(
            "/v1/threads",
            headers={"X-Request-ID": "persistence-unavailable-1"},
        )

    assert response.status_code == 503
    assert response.headers["X-Request-ID"] == "persistence-unavailable-1"
    assert response.json() == {
        "schema_version": "2",
        "request_id": "persistence-unavailable-1",
        "error": {
            "code": "PERSISTENCE_UNAVAILABLE",
            "message": (
                "데이터 저장소를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
            ),
            "retryable": True,
            "detail": {},
        },
    }
    warning.assert_called_once_with(
        "Persistence request failed: type=%s public_code=%s retryable=%s",
        type(failure).__name__,
        "PERSISTENCE_UNAVAILABLE",
        True,
    )
    logged_arguments = repr(warning.call_args)
    assert "sensitive database detail" not in logged_arguments
    assert "private question" not in logged_arguments


@pytest.mark.asyncio
async def test_unexpected_error_uses_safe_standard_envelope(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeMcpGateway(brief_error=RuntimeError("sensitive internal detail"))
    app = create_app(settings=settings, mcp_gateway=gateway)
    error = Mock()
    monkeypatch.setattr(app_module.logger, "error", error)

    async with api_client(app, raise_app_exceptions=False) as client:
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
            headers={"X-Request-ID": "unexpected-1"},
        )

    body = response.json()
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "unexpected-1"
    assert body["schema_version"] == "2"
    assert body["request_id"] == "unexpected-1"
    assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert body["error"]["retryable"] is False
    assert "sensitive" not in response.text
    error.assert_called_once_with(
        "Unexpected request failure: type=%s public_code=%s retryable=%s",
        "RuntimeError",
        "INTERNAL_SERVER_ERROR",
        False,
    )
    logged_arguments = repr(error.call_args)
    assert "sensitive internal detail" not in logged_arguments
    assert QUESTION not in logged_arguments
