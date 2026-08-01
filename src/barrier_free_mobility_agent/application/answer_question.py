from __future__ import annotations

from barrier_free_mobility_agent.application.ports import (
    MobilityMcpGateway,
    StructuredTripAgent,
)
from barrier_free_mobility_agent.domain.errors import (
    InvalidQuestionError,
    QuestionTooLongError,
)
from barrier_free_mobility_agent.domain.models import (
    AccessibilityQuestionResult,
    AnswerOutcome,
)


class AnswerAccessibilityQuestion:
    def __init__(
        self,
        gateway: MobilityMcpGateway,
        *,
        request_max_chars: int,
        mcp_input_max_chars: int,
        trip_agent: StructuredTripAgent | None = None,
    ) -> None:
        self._gateway = gateway
        self._request_max_chars = request_max_chars
        self._mcp_input_max_chars = mcp_input_max_chars
        self._trip_agent = trip_agent

    async def execute(self, question: str) -> AnswerOutcome:
        normalized = question.strip()
        if not normalized:
            raise InvalidQuestionError()
        if len(normalized) > self._request_max_chars:
            raise QuestionTooLongError(
                context={
                    "max_chars": self._request_max_chars,
                    "actual_chars": len(normalized),
                }
            )
        if self._trip_agent is not None and self._trip_agent.supports(normalized):
            return await self._trip_agent.execute(normalized)
        if len(normalized) > self._mcp_input_max_chars:
            return AnswerOutcome.question_too_complex()
        result: AccessibilityQuestionResult = await self._gateway.answer_accessibility_question(
            normalized
        )
        return AnswerOutcome.from_mcp_result(result)
