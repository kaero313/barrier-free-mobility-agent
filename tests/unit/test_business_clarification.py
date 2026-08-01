from __future__ import annotations

import pytest
from tests.factories import QUESTION
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner
from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.application.conversations import ConversationService
from barrier_free_mobility_agent.config import Settings
from barrier_free_mobility_agent.domain.models import ResponseStatus
from barrier_free_mobility_agent.persistence.memory import MemoryConversationRepository


@pytest.mark.asyncio
async def test_mcp_business_clarification_resumes_the_same_run(
    settings: Settings,
) -> None:
    gateway = FakeMcpGateway()
    successful_result = gateway.result.result
    assert successful_result is not None
    gateway.brief_result = successful_result.model_copy(
        update={
            "status": ResponseStatus.NEEDS_CLARIFICATION,
            "clarification_needed": True,
            "questions": ["엘리베이터만 이용해야 하나요?"],
            "user_message": "안전한 판단을 위해 이동 조건을 더 확인해야 합니다.",
        }
    )
    runner = MobilityGraphRunner(gateway, settings)
    service = ConversationService(
        runner,
        MemoryConversationRepository(),
        max_threads=4,
        max_runs_per_thread=4,
        request_max_chars=settings.api_request_max_chars,
        flow_version=settings.agent_flow_version,
        state_schema_version=settings.agent_state_schema_version,
    )
    thread_id = await service.create_thread()

    interrupted = await service.start_run(thread_id, QUESTION)

    assert interrupted.status == GraphTurnStatus.INTERRUPTED
    assert interrupted.resume_mode == ResumeMode.NEW_TURN
    assert interrupted.outcome.status == ResponseStatus.NEEDS_CLARIFICATION

    gateway.brief_result = successful_result
    completed = await service.resume_run(
        interrupted.run_id,
        "네",
    )

    assert completed.run_id == interrupted.run_id
    assert completed.status == GraphTurnStatus.COMPLETED
    assert completed.resume_mode == ResumeMode.NONE
    assert completed.outcome.status == ResponseStatus.SUCCESS
    assert len(gateway.brief_calls) == 2
    assert gateway.brief_calls[-1][2].need_elevator_only is True
