from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from barrier_free_mobility_agent.domain.models import (
    AnswerEvidence,
    AnswerOutcome,
    AnswerPresentation,
    AnswerSource,
    ClarificationOption,
    ClarificationView,
    QuestionIntent,
    ResponseStatus,
)


class AnswerQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=20_000)


class AnswerQuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["3"]
    request_id: str
    source: AnswerSource
    status: ResponseStatus
    intent: QuestionIntent
    user_message: str
    clarification_needed: bool
    clarification: ClarificationView | None
    questions: list[str] = Field(json_schema_extra={"deprecated": True})
    clarification_options: list[ClarificationOption] = Field(json_schema_extra={"deprecated": True})
    available_partial_info: list[str]
    evidence: AnswerEvidence
    presentation: AnswerPresentation | None

    @classmethod
    def from_result(
        cls,
        *,
        request_id: str,
        outcome: AnswerOutcome,
    ) -> AnswerQuestionResponse:
        return cls(
            schema_version=outcome.schema_version,
            request_id=request_id,
            source=outcome.source,
            status=outcome.status,
            intent=outcome.intent,
            user_message=outcome.user_message,
            clarification_needed=outcome.clarification_needed,
            clarification=outcome.clarification,
            questions=outcome.questions,
            clarification_options=outcome.clarification_options,
            available_partial_info=outcome.available_partial_info,
            evidence=outcome.evidence,
            presentation=outcome.presentation,
        )
