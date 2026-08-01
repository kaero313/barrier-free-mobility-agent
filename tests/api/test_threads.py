from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.factories import (
    facility_clarification_result,
    facility_question_result,
    trip_question_result,
)
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.api.app import create_app
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.errors import (
    ExecutionWorkerUnavailableError,
    McpTimeoutError,
)
from barrier_free_mobility_agent.domain.models import (
    ResponseStatus,
    Station,
    StationResolutionResult,
)


@asynccontextmanager
async def api_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        yield client


@pytest.mark.asyncio
async def test_thread_run_interrupt_resume_and_get(settings: Settings) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        created = await client.post("/v1/threads", headers={"X-Request-ID": "thread-1"})
        thread_id = created.json()["thread_id"]
        interrupted = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "홍대입구역에서 삼성역까지 갈 수 있어?"},
        )
        interrupted_body = interrupted.json()
        run_id = interrupted_body["run_id"]
        resumed = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={
                "answer": "계단과 에스컬레이터 모두 이용할 수 없어요",
                "clarification_id": interrupted_body["clarification"]["id"],
            },
        )
        fetched = await client.get(f"/v1/runs/{run_id}")

    assert created.status_code == 201
    assert created.json()["request_id"] == "thread-1"
    assert created.json()["persistence"] == "ephemeral"
    assert interrupted.status_code == 200
    assert interrupted_body["schema_version"] == "3"
    assert interrupted_body["run_status"] == "interrupted"
    assert interrupted_body["status"] == "NEEDS_CLARIFICATION"
    assert interrupted_body["clarification"]["control"] == "free_text"
    assert interrupted_body["clarification"]["answer_mode"] == "single_text"
    assert interrupted_body["clarification"]["prompts"][0]["fields"] == [
        "can_use_stairs",
        "can_use_escalator",
    ]
    assert interrupted_body["context"]["origin"]["status"] == "provided"
    assert interrupted_body["context"]["destination"]["status"] == "provided"
    assert (
        interrupted_body["context"]["active_clarification"]["id"]
        == (interrupted_body["clarification"]["id"])
    )
    assert gateway.resolve_calls == ["홍대입구", "삼성"]
    assert len(gateway.brief_calls) == 1
    assert resumed.status_code == 200
    resumed_body = resumed.json()
    assert resumed_body["run_status"] == "completed"
    assert resumed_body["clarification"] is None
    assert resumed_body["context"]["active_clarification"] is None
    assert resumed_body["context"]["origin"]["status"] == "resolved"
    assert resumed_body["context"]["destination"]["status"] == "resolved"
    assert resumed_body["presentation"]["trip"]["route_summary"]
    assert resumed_body["presentation"]["safety_notice"]
    assert fetched.json()["run_status"] == "completed"
    assert fetched.json()["user_message"] == resumed.json()["user_message"]
    assert fetched.json()["context"] == resumed_body["context"]
    assert fetched.json()["presentation"] == resumed_body["presentation"]


@pytest.mark.asyncio
async def test_invalid_idempotency_key_is_rejected_without_echoing_it(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)
    raw_key = "private key value must not be reflected"

    async with api_client(app) as client:
        created = await client.post("/v1/threads")
        response = await client.post(
            f"/v1/threads/{created.json()['thread_id']}/runs",
            headers={"Idempotency-Key": raw_key},
            json={"question": "시청역에서 강남역까지 갈 수 있나요?"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert raw_key not in response.text
    assert gateway.resolve_calls == []


@pytest.mark.asyncio
async def test_valid_idempotency_key_dispatches_to_durable_coordinator(
    settings: Settings,
) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())
    raw_key = "browser-mutation-key-0000000001"
    observed_keys: list[str] = []

    async with api_client(app) as client:
        container = app.state.container

        async def durable_start(
            thread_id: str,
            question: str,
            *,
            idempotency_key: str,
            **kwargs: object,
        ) -> object:
            observed_keys.append(idempotency_key)
            return await container.conversations.start_run(
                thread_id,
                question,
                owner=kwargs["owner"],
                request_id=str(kwargs["request_id"]),
            )

        app.state.container = replace(
            container,
            durable_conversations=SimpleNamespace(start_run=durable_start),
        )
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        response = await client.post(
            f"/v1/threads/{thread_id}/runs",
            headers={"Idempotency-Key": raw_key},
            json={"question": "시청역에서 강남역까지 갈 수 있나요?"},
        )

    assert response.status_code == 200
    assert observed_keys == [raw_key]
    assert raw_key not in response.text


@pytest.mark.asyncio
async def test_unavailable_execution_worker_is_a_definite_redacted_503(
    settings: Settings,
) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())
    sentinel = "private-worker-identity"

    async with api_client(app) as client:
        container = app.state.container

        async def unavailable_start(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise ExecutionWorkerUnavailableError(context={"worker_id": sentinel})

        app.state.container = replace(
            container,
            durable_conversations=SimpleNamespace(start_run=unavailable_start),
        )
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        response = await client.post(
            f"/v1/threads/{thread_id}/runs",
            headers={"Idempotency-Key": "browser-mutation-key-worker-down"},
            json={"question": "시청역에서 강남역까지 갈 수 있나요?"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "EXECUTION_WORKER_UNAVAILABLE",
        "message": ExecutionWorkerUnavailableError.public_message,
        "retryable": True,
        "detail": {},
    }
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert sentinel not in response.text


@pytest.mark.asyncio
async def test_state_input_budget_error_redacts_internal_budget_context(
    settings: Settings,
) -> None:
    app = create_app(
        settings=settings.model_copy(update={"agent_state_max_bytes": 1024}),
        mcp_gateway=FakeMcpGateway(),
    )

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        response = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "🚉" * 300},
        )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "AGENT_BUDGET_EXCEEDED"
    assert response.json()["schema_version"] == "2"
    assert response.json()["error"]["detail"] == {}


@pytest.mark.asyncio
async def test_facility_run_uses_intent_scoped_context_and_presentation(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        trip = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={
                "question": (
                    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                    "홍대입구역에서 삼성역까지 갈 수 있어?"
                )
            },
        )
        gateway.result = facility_question_result()
        facility = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "강남역 엘리베이터 운행 중이야?"},
        )
        historical_trip = await client.get(f"/v1/runs/{trip.json()['run_id']}")

    body = facility.json()
    assert facility.status_code == 200
    assert body["schema_version"] == "3"
    assert body["run_status"] == "completed"
    assert body["status"] == "SUCCESS"
    assert body["intent"] == "facility_status"
    assert body["context"]["intent"] == "facility_status"
    assert body["context"]["origin"] is None
    assert body["context"]["destination"] is None
    assert body["context"]["mobility_conditions"] == []
    assert body["context"]["facility"]["target"]["station_name"] == "강남"
    assert body["presentation"]["kind"] == "facility"
    assert body["presentation"]["facility"]["items"][0]["answer_state"] == "AVAILABLE"
    assert historical_trip.json()["context"] == trip.json()["context"]


@pytest.mark.asyncio
async def test_facility_clarification_resumes_with_current_id(settings: Settings) -> None:
    gateway = FakeMcpGateway(result=facility_clarification_result())
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        started = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "어느 역 엘리베이터 운행 상태를 알려줘"},
        )
        gateway.result = facility_question_result()
        resumed = await client.post(
            f"/v1/runs/{started.json()['run_id']}/resume",
            json={
                "answer": "강남역",
                "clarification_id": started.json()["clarification"]["id"],
            },
        )

    assert started.json()["run_status"] == "interrupted"
    assert started.json()["intent"] == "facility_status"
    assert started.json()["clarification"]["prompts"][0]["fields"] == ["target_station"]
    assert resumed.json()["run_status"] == "completed"
    assert resumed.json()["presentation"]["kind"] == "facility"


@pytest.mark.asyncio
async def test_resume_rejects_a_stale_clarification_id(settings: Settings) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        started = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "홍대입구역에서 삼성역까지 갈 수 있어?"},
        )
        run_id = started.json()["run_id"]
        stale = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={"answer": "둘 다 못 써요", "clarification_id": "old-screen"},
        )
        unchanged = await client.get(f"/v1/runs/{run_id}")

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_CLARIFICATION"
    assert stale.json()["error"]["detail"] == {}
    assert unchanged.json()["run_status"] == "interrupted"
    assert unchanged.json()["clarification"]["id"] == started.json()["clarification"]["id"]
    assert gateway.resolve_calls == []


@pytest.mark.asyncio
async def test_interrupted_thread_rejects_a_competing_run(settings: Settings) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        first = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "홍대입구역에서 삼성역까지 갈 수 있어?"},
        )
        competing = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "휠체어로 서울역에서 강남역까지 갈 수 있어?"},
        )

    assert first.json()["run_status"] == "interrupted"
    assert competing.status_code == 409
    assert competing.json()["error"]["code"] == "THREAD_BUSY"
    assert competing.json()["error"]["detail"] == {}
    assert thread_id not in competing.text


@pytest.mark.asyncio
async def test_completed_thread_uses_previous_destination_for_deictic_followup(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        first = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={
                "question": (
                    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                    "홍대입구역에서 삼성역까지 갈 수 있어?"
                )
            },
        )
        followup = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "거기서 시청역까지는?"},
        )
        first_after_followup = await client.get(f"/v1/runs/{first.json()['run_id']}")

    assert first.json()["run_status"] == "completed"
    assert followup.status_code == 200
    assert followup.json()["run_status"] == "completed"
    assert first.json()["context"]["origin"]["station_name"] == "홍대입구"
    assert first.json()["context"]["destination"]["station_name"] == "삼성"
    assert followup.json()["context"]["origin"]["station_name"] == "삼성"
    assert followup.json()["context"]["destination"]["station_name"] == "시청"
    assert first_after_followup.json()["context"] == first.json()["context"]
    assert gateway.resolve_calls == ["홍대입구", "삼성", "삼성", "시청"]
    assert [(origin, destination) for origin, destination, _ in gateway.brief_calls] == [
        ("홍대입구", "삼성"),
        ("삼성", "시청"),
    ]


@pytest.mark.asyncio
async def test_unrelated_followup_does_not_reexecute_retained_trip(settings: Settings) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        first = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={
                "question": (
                    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                    "홍대입구역에서 삼성역까지 갈 수 있어?"
                )
            },
        )
        followup = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "고마워"},
        )
        resumed = await client.post(
            f"/v1/runs/{followup.json()['run_id']}/resume",
            json={"answer": "okay"},
        )

    assert first.json()["run_status"] == "completed"
    assert followup.json()["run_status"] == "interrupted"
    assert followup.json()["intent"] == "unknown"
    assert resumed.json()["run_status"] == "interrupted"
    assert resumed.json()["intent"] == "unknown"
    assert gateway.resolve_calls == ["홍대입구", "삼성"]
    assert len(gateway.brief_calls) == 1


@pytest.mark.asyncio
async def test_station_clarification_exposes_safe_selection_options(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway(
        station_results={
            "시청": StationResolutionResult(
                query="시청",
                matched_station=None,
                candidates=[
                    Station(station_name="시청", line="1호선"),
                    Station(station_name="시청", line="2호선"),
                ],
                needs_clarification=True,
                clarification_message="시청역 노선을 선택해 주세요.",
            )
        }
    )
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        response = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={
                "question": (
                    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                    "시청역에서 삼성역까지 갈 수 있어?"
                )
            },
        )

    assert response.json()["clarification_options"] == [
        {"value": "시청 (1호선)", "label": "시청 (1호선)"},
        {"value": "시청 (2호선)", "label": "시청 (2호선)"},
    ]
    assert response.json()["clarification"]["control"] == "choice"
    assert response.json()["clarification"]["prompts"][0]["fields"] == ["origin"]
    assert response.json()["clarification"]["options"] == response.json()["clarification_options"]


@pytest.mark.asyncio
async def test_mcp_clarification_can_resume_the_same_run(settings: Settings) -> None:
    canonical = trip_question_result()
    assert canonical.result is not None
    clarification = canonical.result.model_copy(
        update={
            "status": ResponseStatus.NEEDS_CLARIFICATION,
            "clarification_needed": True,
            "questions": ["엘리베이터만 이용해야 하나요?"],
            "user_message": "이동 조건을 한 가지 더 확인해야 합니다.",
        }
    )
    gateway = FakeMcpGateway(brief_result=clarification)
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        started = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={
                "question": (
                    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                    "홍대입구역에서 삼성역까지 갈 수 있어?"
                )
            },
        )
        gateway.brief_result = canonical.result
        resumed = await client.post(
            f"/v1/runs/{started.json()['run_id']}/resume",
            json={
                "answer": "네",
                "clarification_id": started.json()["clarification"]["id"],
            },
        )

    assert started.json()["run_status"] == "interrupted"
    assert started.json()["status"] == "NEEDS_CLARIFICATION"
    assert started.json()["clarification"]["id"] == "business-1"
    assert started.json()["clarification"]["prompts"][0]["fields"] == ["need_elevator_only"]
    assert resumed.json()["run_status"] == "completed"
    assert len(gateway.brief_calls) == 2


@pytest.mark.asyncio
async def test_failed_resume_is_terminal_and_preserves_reserved_tool_attempt(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        started = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "홍대입구역에서 삼성역까지 갈 수 있어?"},
        )
        gateway.brief_error = McpTimeoutError()
        failed_response = await client.post(
            f"/v1/runs/{started.json()['run_id']}/resume",
            json={"answer": "계단과 에스컬레이터를 모두 이용할 수 없어요"},
        )
        failed_run = await client.get(f"/v1/runs/{started.json()['run_id']}")
        checkpoint = await app.state.container.trip_agent.get_state(thread_id)
        gateway.brief_error = None
        retried = await client.post(
            f"/v1/runs/{started.json()['run_id']}/resume",
            json={"answer": "다시 시도"},
        )

    assert failed_response.status_code == 504
    assert failed_run.json()["run_status"] == "failed"
    assert failed_run.json()["status"] == "FAILED"
    assert checkpoint["tool_ledger"] == [
        "resolve_station",
        "resolve_station",
        "generate_accessibility_brief",
    ]
    assert checkpoint["run_tool_ledger"] == checkpoint["tool_ledger"]
    assert retried.status_code == 409
    assert retried.json()["error"]["code"] == "RUN_NOT_RESUMABLE"
    assert retried.json()["error"]["detail"] == {}
    assert len(gateway.brief_calls) == 1


@pytest.mark.asyncio
async def test_completed_run_cannot_be_resumed(settings: Settings) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        completed = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={
                "question": (
                    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                    "홍대입구역에서 삼성역까지 갈 수 있어?"
                )
            },
        )
        response = await client.post(
            f"/v1/runs/{completed.json()['run_id']}/resume",
            json={"answer": "다시 실행"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_NOT_RESUMABLE"
    assert response.json()["error"]["detail"] == {}


@pytest.mark.asyncio
async def test_delete_thread_removes_runs_and_checkpoint(settings: Settings) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        run = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={
                "question": (
                    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                    "홍대입구역에서 삼성역까지 갈 수 있어?"
                )
            },
        )
        run_id = run.json()["run_id"]
        deleted = await client.delete(f"/v1/threads/{thread_id}")
        missing_run = await client.get(f"/v1/runs/{run_id}")
        missing_thread = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "휠체어로 서울역에서 강남역까지 갈 수 있어?"},
        )

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert missing_run.status_code == 404
    assert missing_run.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert missing_run.json()["error"]["detail"] == {}
    assert run_id not in missing_run.text
    assert missing_thread.status_code == 404
    assert missing_thread.json()["error"]["code"] == "THREAD_NOT_FOUND"
    assert missing_thread.json()["error"]["detail"] == {}
    assert thread_id not in missing_thread.text


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_answer", ["   ", "가" * 2001], ids=["blank", "too-long"])
async def test_invalid_resume_input_keeps_run_resumable(
    settings: Settings,
    invalid_answer: str,
) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        interrupted = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "홍대입구역에서 삼성역까지 갈 수 있어?"},
        )
        response = await client.post(
            f"/v1/runs/{interrupted.json()['run_id']}/resume",
            json={"answer": invalid_answer},
        )
        unchanged = await client.get(f"/v1/runs/{interrupted.json()['run_id']}")
        completed = await client.post(
            f"/v1/runs/{interrupted.json()['run_id']}/resume",
            json={"answer": "계단과 에스컬레이터를 모두 이용할 수 없어요"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] in {"INVALID_QUESTION", "QUESTION_TOO_LONG"}
    assert unchanged.json()["run_status"] == "interrupted"
    assert completed.json()["run_status"] == "completed"


def test_openapi_exposes_agent_contract_without_raw_mcp_models(settings: Settings) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    schema = app.openapi()
    paths = schema["paths"]
    component_names = set(schema["components"]["schemas"])

    assert "/v1/threads" in paths
    assert "/v1/threads/{thread_id}/runs" in paths
    assert "/v1/runs/{run_id}/resume" in paths
    assert "AccessibilityResult" not in component_names
    assert "AccessibilityQuestionResult" not in component_names
    assert "MobilityProfile" not in component_names
    assert "StationResolutionResult" not in component_names
    assert "AgentState" not in component_names
    assert "TripContext" not in component_names
    assert "MobilityContext" not in component_names
    assert "PendingClarification" not in component_names
    assert "AccessibleFacility" not in component_names
    assert "FacilityQuestionResult" not in component_names
    assert {
        "TripAnswerPresentation",
        "FacilityAnswerPresentation",
        "ClarificationView",
        "ConversationContextView",
        "ConversationRunResponse",
        "ConversationRunResponseV4",
        "StructuredClarificationView",
    } <= component_names
    for path, method in (
        ("/v1/threads/{thread_id}/runs", "post"),
        ("/v1/runs/{run_id}", "get"),
        ("/v1/runs/{run_id}/resume", "post"),
    ):
        response_schema = paths[path][method]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert {item["$ref"] for item in response_schema["anyOf"]} == {
            "#/components/schemas/ConversationRunResponse",
            "#/components/schemas/ConversationRunResponseV4",
        }
        response_header = next(
            parameter
            for parameter in paths[path][method]["parameters"]
            if parameter["name"] == "X-Agent-Response-Schema"
        )
        assert response_header["in"] == "header"
        assert response_header["schema"]["type"] == "string"
        assert response_header["schema"]["enum"] == ["3", "4"]
        assert response_header["schema"]["default"] == "3"
        assert response_header["schema"]["title"] == "X-Agent-Response-Schema"
    for path in (
        "/v1/threads/{thread_id}/runs",
        "/v1/runs/{run_id}/resume",
    ):
        idempotency_header = next(
            parameter
            for parameter in paths[path]["post"]["parameters"]
            if parameter["name"] == "Idempotency-Key"
        )
        assert idempotency_header["in"] == "header"
        assert idempotency_header["required"] is False
        key_schema = idempotency_header["schema"]["anyOf"][0]
        assert key_schema["minLength"] == 16
        assert key_schema["maxLength"] == 128
    run_schema = schema["components"]["schemas"]["ConversationRunResponse"]
    assert run_schema["additionalProperties"] is False
    assert {"clarification", "presentation", "context"} <= set(run_schema["required"])
    assert run_schema["properties"]["questions"]["deprecated"] is True
    assert run_schema["properties"]["clarification_options"]["deprecated"] is True
    structured_run_schema = schema["components"]["schemas"]["ConversationRunResponseV4"]
    assert structured_run_schema["additionalProperties"] is False
    assert {"clarification", "presentation", "context"} <= set(structured_run_schema["required"])
    assert "questions" not in structured_run_schema["properties"]
    assert "clarification_options" not in structured_run_schema["properties"]
    presentation_union = run_schema["properties"]["presentation"]["anyOf"][0]
    assert presentation_union["discriminator"]["propertyName"] == "kind"
    assert presentation_union["discriminator"]["mapping"] == {
        "facility": "#/components/schemas/FacilityAnswerPresentation",
        "trip": "#/components/schemas/TripAnswerPresentation",
    }
    resume_request = schema["components"]["schemas"]["ResumeRunRequest"]
    assert "schema_version" in resume_request["properties"]
    assert "clarification_id" in resume_request["properties"]
    assert "clarification_id" not in resume_request["required"]
    assert resume_request["required"] == ["answer"]
    resume_responses = paths["/v1/runs/{run_id}/resume"]["post"]["responses"]
    assert set(resume_responses) == {
        "200",
        "401",
        "404",
        "409",
        "422",
        "429",
        "500",
        "502",
        "503",
        "504",
    }
    assert resume_responses["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert "ErrorResponse" in component_names


@pytest.mark.asyncio
async def test_ephemeral_thread_capacity_is_bounded(settings: Settings) -> None:
    bounded = settings.model_copy(update={"agent_max_threads_per_owner": 1})
    app = create_app(settings=bounded, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        first = await client.post("/v1/threads")
        second = await client.post("/v1/threads")

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "AGENT_CAPACITY_EXCEEDED"
    assert second.json()["error"]["detail"] == {}


@pytest.mark.asyncio
async def test_runs_per_thread_capacity_is_bounded(settings: Settings) -> None:
    bounded = settings.model_copy(update={"agent_max_runs_per_thread": 1})
    app = create_app(settings=bounded, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        first = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={
                "question": (
                    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                    "홍대입구역에서 삼성역까지 갈 수 있어?"
                )
            },
        )
        second = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "휠체어로 서울역에서 강남역까지 갈 수 있어?"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "AGENT_CAPACITY_EXCEEDED"
    assert second.json()["error"]["detail"] == {}
