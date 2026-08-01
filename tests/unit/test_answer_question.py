from __future__ import annotations

import pytest
from tests.factories import QUESTION
from tests.fakes import FakeMcpGateway

from barrier_free_mobility_agent.application.answer_question import (
    AnswerAccessibilityQuestion,
)
from barrier_free_mobility_agent.domain.errors import (
    InvalidQuestionError,
    QuestionTooLongError,
)
from barrier_free_mobility_agent.domain.models import AnswerSource, ResponseStatus


@pytest.mark.asyncio
async def test_valid_question_calls_mcp_once() -> None:
    gateway = FakeMcpGateway()
    use_case = AnswerAccessibilityQuestion(
        gateway,
        request_max_chars=2000,
        mcp_input_max_chars=120,
    )

    outcome = await use_case.execute(f"  {QUESTION}  ")

    assert gateway.answer_calls == [QUESTION]
    assert outcome.source == AnswerSource.MCP


@pytest.mark.asyncio
async def test_question_over_mcp_limit_returns_agent_clarification() -> None:
    gateway = FakeMcpGateway()
    use_case = AnswerAccessibilityQuestion(
        gateway,
        request_max_chars=2000,
        mcp_input_max_chars=120,
    )

    outcome = await use_case.execute("가" * 121)

    assert gateway.answer_calls == []
    assert outcome.source == AnswerSource.AGENT
    assert outcome.status == ResponseStatus.NEEDS_CLARIFICATION
    assert outcome.clarification_needed is True
    assert outcome.questions


@pytest.mark.asyncio
async def test_question_over_api_limit_is_rejected() -> None:
    gateway = FakeMcpGateway()
    use_case = AnswerAccessibilityQuestion(
        gateway,
        request_max_chars=200,
        mcp_input_max_chars=120,
    )

    with pytest.raises(QuestionTooLongError):
        await use_case.execute("가" * 201)

    assert gateway.answer_calls == []


@pytest.mark.asyncio
async def test_blank_question_is_rejected() -> None:
    gateway = FakeMcpGateway()
    use_case = AnswerAccessibilityQuestion(
        gateway,
        request_max_chars=2000,
        mcp_input_max_chars=120,
    )

    with pytest.raises(InvalidQuestionError):
        await use_case.execute("   ")
