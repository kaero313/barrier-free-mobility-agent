from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from tests.factories import QUESTION, mobility_profile

from barrier_free_mobility_agent.api.app import create_app
from barrier_free_mobility_agent.config import AppEnvironment, Settings
from barrier_free_mobility_agent.domain.models import (
    McpCapability,
    MobilityProfile,
    QuestionIntent,
    ResponseStatus,
)
from barrier_free_mobility_agent.integrations.mcp.gateway import LangChainMcpGateway

EXPECTED_TOOLS = {
    "resolve_station",
    "get_station_facilities",
    "get_elevator_status",
    "get_accessible_restroom",
    "get_route_candidates",
    "check_accessible_trip",
    "generate_accessibility_brief",
    "answer_accessibility_question",
}


def contract_settings() -> Settings:
    mcp_url = os.getenv("MCP_CONTRACT_URL")
    health_url = os.getenv("MCP_CONTRACT_HEALTH_URL")
    if not mcp_url or not health_url:
        pytest.skip("Set MCP_CONTRACT_URL and MCP_CONTRACT_HEALTH_URL to run contract tests.")
    return Settings(
        _env_file=None,
        app_env=AppEnvironment.TEST,
        mcp_mobility_url=mcp_url,
        mcp_mobility_health_url=health_url,
        mcp_connect_timeout_seconds=5,
        mcp_tool_timeout_seconds=60,
        mcp_contract_refresh_seconds=60,
    )


@pytest.mark.contract
@pytest.mark.asyncio
async def test_running_mcp_contract_and_fastapi_vertical_slice() -> None:
    settings = contract_settings()
    gateway = LangChainMcpGateway(settings)
    app = create_app(settings=settings, mcp_gateway=gateway)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        snapshot = gateway.contract_snapshot
        ready = await client.get("/health/ready")
        response = await client.post(
            "/v1/accessibility/questions",
            json={"question": QUESTION},
            headers={"X-Request-ID": "contract-e2e"},
        )
        station = await gateway.resolve_station("홍대입구")
        brief = await gateway.generate_accessibility_brief(
            "홍대입구",
            "삼성",
            MobilityProfile.model_validate(mobility_profile()),
        )
        facility = await gateway.answer_accessibility_question("강남역 엘리베이터 운행 중이야?")
        clarification = await gateway.answer_accessibility_question("휠체어로 갈 수 있어?")
        thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        interrupted_run = await client.post(
            f"/v1/threads/{thread_id}/runs",
            json={"question": "홍대입구역에서 삼성역까지 갈 수 있어?"},
        )
        resumed_run = await client.post(
            f"/v1/runs/{interrupted_run.json()['run_id']}/resume",
            json={
                "answer": "계단과 에스컬레이터를 모두 이용할 수 없어요",
                "clarification_id": interrupted_run.json()["clarification"]["id"],
            },
        )
        facility_thread_id = (await client.post("/v1/threads")).json()["thread_id"]
        facility_run = await client.post(
            f"/v1/threads/{facility_thread_id}/runs",
            json={"question": "2호선 강남역 엘리베이터 운행 상태 알려줘"},
        )

    body = response.json()
    assert set(snapshot.tool_names) == EXPECTED_TOOLS
    assert snapshot.schema_fingerprint is not None
    assert len(snapshot.schema_fingerprint) == 64
    assert ready.status_code == 200
    assert set(ready.json()["dependencies"]["mobility_mcp"]["capabilities"]) == {
        capability.value for capability in snapshot.capabilities
    }
    assert response.status_code == 200
    assert body["request_id"] == "contract-e2e"
    assert body["source"] == "mcp"
    assert body["user_message"]
    assert body["evidence"]["risk_level"] in {"LOW", "CAUTION", "HIGH", "UNKNOWN"}
    assert station.matched_station is not None
    assert station.matched_station.station_name == "홍대입구"
    assert brief.origin == "홍대입구"
    assert brief.destination == "삼성"
    assert brief.user_message
    assert brief.risk_level.value in {"LOW", "CAUTION", "HIGH", "UNKNOWN"}
    if facility.status == ResponseStatus.NEEDS_CLARIFICATION:
        assert facility.clarification_needed is True
        assert facility.questions
    else:
        assert facility.intent == QuestionIntent.FACILITY_STATUS
        assert facility.facility_result is not None
        assert facility.user_message == facility.facility_result.user_message
    assert clarification.status == ResponseStatus.NEEDS_CLARIFICATION
    assert clarification.clarification_needed is True
    assert clarification.questions
    assert interrupted_run.status_code == 200
    assert interrupted_run.json()["run_status"] == "interrupted"
    assert interrupted_run.json()["status"] == "NEEDS_CLARIFICATION"
    assert resumed_run.status_code == 200
    assert resumed_run.json()["run_status"] == "completed"
    assert resumed_run.json()["source"] == "mcp"
    assert facility_run.status_code == 200
    assert facility_run.json()["run_status"] == "completed"
    if McpCapability.FACILITY_QUESTION in snapshot.capabilities:
        assert facility_run.json()["intent"] == "facility_status"
        assert facility_run.json()["presentation"]["kind"] == "facility"
        assert facility_run.json()["context"]["facility"]["target"] is not None
    else:
        assert facility_run.json()["status"] == "FAILED"
        assert facility_run.json()["source"] == "agent"
        assert facility_run.json()["presentation"] is None
