from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from langgraph.checkpoint.base import BaseCheckpointSaver

from barrier_free_mobility_agent.agent.graph import MobilityGraphRunner
from barrier_free_mobility_agent.application.answer_question import (
    AnswerAccessibilityQuestion,
)
from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationRepository,
)
from barrier_free_mobility_agent.application.conversations import ConversationService
from barrier_free_mobility_agent.application.durable_conversations import (
    DurableConversationCoordinator,
    DurableConversationOptions,
)
from barrier_free_mobility_agent.application.ports import MobilityMcpGateway
from barrier_free_mobility_agent.config import Settings


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    settings: Settings
    mcp_gateway: MobilityMcpGateway
    trip_agent: MobilityGraphRunner
    conversations: ConversationService
    durable_conversations: DurableConversationCoordinator | None
    answer_question: AnswerAccessibilityQuestion


def build_container(
    settings: Settings,
    mcp_gateway: MobilityMcpGateway,
    *,
    checkpointer: BaseCheckpointSaver[str],
    conversation_repository: ConversationRepository,
) -> ApplicationContainer:
    trip_agent = MobilityGraphRunner(
        mcp_gateway,
        settings,
        checkpointer=checkpointer,
        run_ledger=conversation_repository,
    )
    idempotency_key = settings.idempotency_hmac_key
    durable_conversations = (
        DurableConversationCoordinator(
            conversation_repository,
            idempotency_hmac_key=idempotency_key.get_secret_value().encode("utf-8"),
            max_runs_per_thread=settings.agent_max_runs_per_thread,
            request_max_chars=settings.api_request_max_chars,
            flow_version=settings.agent_flow_version,
            state_schema_version=settings.agent_state_schema_version,
            options=DurableConversationOptions(
                bounded_wait=timedelta(seconds=settings.execution_bounded_wait_seconds),
                poll_interval=timedelta(seconds=settings.execution_poll_interval_seconds),
            ),
        )
        if conversation_repository.persistence == "durable" and idempotency_key is not None
        else None
    )
    return ApplicationContainer(
        settings=settings,
        mcp_gateway=mcp_gateway,
        trip_agent=trip_agent,
        conversations=ConversationService(
            trip_agent,
            conversation_repository,
            max_threads=settings.agent_max_threads_per_owner,
            max_runs_per_thread=settings.agent_max_runs_per_thread,
            request_max_chars=settings.api_request_max_chars,
            flow_version=settings.agent_flow_version,
            state_schema_version=settings.agent_state_schema_version,
            max_total_threads=settings.agent_max_total_threads,
        ),
        durable_conversations=durable_conversations,
        answer_question=AnswerAccessibilityQuestion(
            mcp_gateway,
            request_max_chars=settings.api_request_max_chars,
            mcp_input_max_chars=settings.mcp_tool_input_max_chars,
            trip_agent=trip_agent,
        ),
    )
