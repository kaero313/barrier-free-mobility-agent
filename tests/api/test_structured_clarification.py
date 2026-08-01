from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.api.app import create_app
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.models import Station, StationResolutionResult


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


def _structured_trip_answer(clarification_id: str) -> dict[str, Any]:
    return {
        "schema_version": "2",
        "clarification_id": clarification_id,
        "answer": {
            "mode": "fields",
            "values": [
                {"input_id": "origin", "value": {"kind": "text", "value": "홍대입구"}},
                {"input_id": "destination", "value": {"kind": "text", "value": "삼성"}},
                {
                    "input_id": "can_use_stairs",
                    "value": {"kind": "tri_state", "value": "no"},
                },
                {
                    "input_id": "can_use_escalator",
                    "value": {"kind": "tri_state", "value": "yes"},
                },
            ],
        },
    }


async def _start_required_fields_run(
    client: AsyncClient,
    *,
    response_schema: str | None = "4",
) -> dict[str, Any]:
    thread_id = (await client.post("/v1/threads")).json()["thread_id"]
    headers = {"X-Agent-Response-Schema": response_schema} if response_schema else None
    response = await client.post(
        f"/v1/threads/{thread_id}/runs",
        json={"question": "휠체어로 갈 수 있어?"},
        headers=headers,
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


@pytest.mark.asyncio
async def test_run_response_defaults_to_v3_and_header_opts_in_to_v4(
    settings: Settings,
) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        legacy = await _start_required_fields_run(client, response_schema=None)
        structured_response = await client.get(
            f"/v1/runs/{legacy['run_id']}",
            headers={"X-Agent-Response-Schema": "4"},
        )

    structured = structured_response.json()
    assert legacy["schema_version"] == "3"
    assert legacy["clarification"]["schema_version"] == "1"
    assert legacy["clarification"]["answer_mode"] == "single_text"
    assert "questions" in legacy
    assert "clarification_options" in legacy

    assert structured_response.status_code == 200
    assert structured["schema_version"] == "4"
    assert structured["clarification"]["schema_version"] == "2"
    assert structured["clarification"]["answer_request"]["mode"] == "fields"
    assert [item["id"] for item in structured["clarification"]["answer_request"]["inputs"]] == [
        "origin",
        "destination",
        "can_use_stairs",
        "can_use_escalator",
    ]
    assert "questions" not in structured
    assert "clarification_options" not in structured


@pytest.mark.asyncio
async def test_legacy_string_resume_remains_supported(settings: Settings) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        started = await _start_required_fields_run(client)
        resumed = await client.post(
            f"/v1/runs/{started['run_id']}/resume",
            json={
                "answer": (
                    "홍대입구역에서 삼성역까지, 계단은 이용할 수 없고 "
                    "에스컬레이터는 이용할 수 있어요"
                ),
                "clarification_id": started["clarification"]["id"],
            },
        )

    assert resumed.status_code == 200
    assert resumed.json()["schema_version"] == "3"
    assert resumed.json()["run_status"] == "completed"
    assert len(gateway.brief_calls) == 1


@pytest.mark.asyncio
async def test_structured_resume_shape_completes_with_v4_response(settings: Settings) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        started = await _start_required_fields_run(client)
        resumed = await client.post(
            f"/v1/runs/{started['run_id']}/resume",
            json=_structured_trip_answer(started["clarification"]["id"]),
            headers={"X-Agent-Response-Schema": "4"},
        )

    assert resumed.status_code == 200
    assert resumed.json()["schema_version"] == "4"
    assert resumed.json()["run_status"] == "completed"
    assert resumed.json()["clarification"] is None
    assert gateway.resolve_calls == ["홍대입구", "삼성"]
    assert gateway.brief_calls[0][2].can_use_stairs is False
    assert gateway.brief_calls[0][2].can_use_escalator is True


@pytest.mark.asyncio
async def test_semantic_422_issues_do_not_advance_the_active_run(settings: Settings) -> None:
    gateway = FakeMcpGateway()
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with api_client(app) as client:
        started = await _start_required_fields_run(client)
        valid = _structured_trip_answer(started["clarification"]["id"])
        invalid_payloads: list[tuple[dict[str, Any], str, list[str]]] = []

        missing = deepcopy(valid)
        missing["answer"]["values"].pop()
        invalid_payloads.append(
            (
                missing,
                "clarification_answer_missing",
                ["body", "answer", "values", "can_use_escalator"],
            )
        )

        duplicate = deepcopy(valid)
        duplicate["answer"]["values"].append(deepcopy(duplicate["answer"]["values"][0]))
        invalid_payloads.append(
            (
                duplicate,
                "clarification_answer_duplicate",
                ["body", "answer", "values", "origin"],
            )
        )

        unexpected = deepcopy(valid)
        unexpected["answer"]["values"].append(
            {"input_id": "unexpected", "value": {"kind": "text", "value": "값"}}
        )
        invalid_payloads.append(
            (
                unexpected,
                "clarification_answer_unexpected",
                ["body", "answer", "values", "unexpected"],
            )
        )

        mismatched = deepcopy(valid)
        mismatched["answer"]["values"][0]["value"] = {
            "kind": "tri_state",
            "value": "yes",
        }
        invalid_payloads.append(
            (
                mismatched,
                "clarification_answer_type_mismatch",
                ["body", "answer", "values", "origin"],
            )
        )

        for payload, issue_type, location in invalid_payloads:
            response = await client.post(
                f"/v1/runs/{started['run_id']}/resume",
                json=payload,
            )
            assert response.status_code == 422
            body = response.json()
            assert body["error"]["code"] == "REQUEST_VALIDATION_ERROR"
            matching = [
                issue for issue in body["error"]["detail"]["issues"] if issue["type"] == issue_type
            ]
            assert matching
            assert matching[0]["location"] == location

        unchanged = await client.get(
            f"/v1/runs/{started['run_id']}",
            headers={"X-Agent-Response-Schema": "4"},
        )

    assert unchanged.json()["run_status"] == "interrupted"
    assert unchanged.json()["clarification"]["id"] == started["clarification"]["id"]
    assert gateway.resolve_calls == []
    assert gateway.brief_calls == []


@pytest.mark.asyncio
async def test_invalid_station_token_returns_semantic_422_and_valid_token_resumes(
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
        started_response = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={
                "question": (
                    "휠체어를 사용하고 계단과 에스컬레이터는 모두 이용할 수 없어요. "
                    "시청역에서 삼성역까지 갈 수 있어?"
                )
            },
            headers={"X-Agent-Response-Schema": "4"},
        )
        started = started_response.json()
        answer = {
            "schema_version": "2",
            "clarification_id": started["clarification"]["id"],
            "answer": {
                "mode": "fields",
                "values": [
                    {
                        "input_id": "origin",
                        "value": {"kind": "choice", "value": "option-3"},
                    }
                ],
            },
        }
        invalid = await client.post(
            f"/v1/runs/{started['run_id']}/resume",
            json=answer,
        )
        answer["answer"]["values"][0]["value"]["value"] = "option-2"
        resumed = await client.post(
            f"/v1/runs/{started['run_id']}/resume",
            json=answer,
            headers={"X-Agent-Response-Schema": "4"},
        )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert invalid.json()["error"]["detail"]["issues"] == [
        {
            "type": "clarification_answer_invalid_option",
            "location": ["body", "answer", "values", "origin"],
            "message": "현재 선택할 수 없는 항목입니다.",
        }
    ]
    assert resumed.status_code == 200
    assert resumed.json()["run_status"] == "completed"
    assert gateway.brief_calls[0][0] == "2호선 시청"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": "2",
            "answer": {"mode": "single_text", "value": "답변"},
        },
        {
            "schema_version": "2",
            "clarification_id": "clarification-id",
            "answer": "legacy string",
        },
        {
            "clarification_id": "clarification-id",
            "answer": {"mode": "single_text", "value": "답변"},
        },
    ],
    ids=["missing-id", "version-two-string", "structured-without-version"],
)
async def test_structured_request_requires_version_id_and_structured_answer(
    settings: Settings,
    payload: dict[str, object],
) -> None:
    app = create_app(settings=settings, mcp_gateway=FakeMcpGateway())

    async with api_client(app) as client:
        started = await _start_required_fields_run(client)
        response = await client.post(
            f"/v1/runs/{started['run_id']}/resume",
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
