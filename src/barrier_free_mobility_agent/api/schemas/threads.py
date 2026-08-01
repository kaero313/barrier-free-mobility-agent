from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from barrier_free_mobility_agent.agent.models import GraphTurnStatus
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.domain.clarification import (
    FieldsClarificationAnswer,
    SingleTextClarificationAnswer,
    StructuredClarificationAnswer,
    StructuredClarificationView,
    project_structured_clarification,
)
from barrier_free_mobility_agent.domain.models import (
    AnswerEvidence,
    AnswerPresentation,
    AnswerSource,
    ClarificationOption,
    ClarificationView,
    ConversationContextView,
    QuestionIntent,
    ResponseStatus,
)


class CreateThreadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    request_id: str
    thread_id: str
    persistence: Literal["ephemeral", "durable"]


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=20_000)


class ResumeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] | None = None
    clarification_id: str | None = Field(default=None, min_length=1, max_length=200)
    answer: Annotated[str, Field(min_length=1, max_length=20_000)] | StructuredClarificationAnswer

    @model_validator(mode="after")
    def validate_versioned_answer(self) -> Self:
        if self.schema_version == "2":
            if self.clarification_id is None:
                raise ValueError("Structured resume requires clarification_id.")
            if not isinstance(
                self.answer,
                (SingleTextClarificationAnswer, FieldsClarificationAnswer),
            ):
                raise ValueError("Schema version 2 requires a structured answer.")
        elif not isinstance(self.answer, str):
            raise ValueError("Structured answers require schema_version 2.")
        return self


class ConversationRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["3"] = "3"
    request_id: str
    thread_id: str
    run_id: str
    run_status: GraphTurnStatus
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
    context: ConversationContextView

    @model_validator(mode="after")
    def validate_run_outcome_matrix(self) -> Self:
        if self.context.intent != self.intent:
            raise ValueError("Run intent and context intent must match.")
        if self.run_status == GraphTurnStatus.INTERRUPTED:
            if self.status != ResponseStatus.NEEDS_CLARIFICATION or self.clarification is None:
                raise ValueError("Interrupted run requires an active clarification.")
        elif self.run_status == GraphTurnStatus.COMPLETED:
            if self.status == ResponseStatus.NEEDS_CLARIFICATION:
                raise ValueError("Completed run cannot require clarification.")
        elif self.status != ResponseStatus.FAILED or self.clarification is not None:
            raise ValueError("Failed run requires a terminal failed outcome.")
        active = self.context.active_clarification
        if self.clarification is not None:
            if active is None or active.id != self.clarification.id:
                raise ValueError("Context must reference the active clarification.")
        elif active is not None:
            raise ValueError("Terminal context cannot retain an active clarification.")
        if (
            self.presentation is not None
            and self.presentation.kind == "facility"
            and self.context.facility is not None
            and self.context.facility.target is not None
            and self.context.facility.target.station_name is not None
            and self.presentation.facility.station_name != self.context.facility.target.station_name
        ):
            raise ValueError("Facility presentation and context target must match.")
        return self

    @classmethod
    def from_run(
        cls,
        *,
        request_id: str,
        run: ConversationRun,
    ) -> ConversationRunResponse:
        outcome = run.outcome
        return cls(
            request_id=request_id,
            thread_id=run.thread_id,
            run_id=run.run_id,
            run_status=run.status,
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
            context=run.context,
        )


class ConversationRunResponseV4(BaseModel):
    """Opt-in run response consumed by the same-product Web BFF."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["4"] = "4"
    request_id: str
    thread_id: str
    run_id: str
    run_status: GraphTurnStatus
    source: AnswerSource
    status: ResponseStatus
    intent: QuestionIntent
    user_message: str
    clarification_needed: bool
    clarification: StructuredClarificationView | None
    available_partial_info: list[str]
    evidence: AnswerEvidence
    presentation: AnswerPresentation | None
    context: ConversationContextView

    @model_validator(mode="after")
    def validate_run_outcome_matrix(self) -> Self:
        if self.context.intent != self.intent:
            raise ValueError("Run intent and context intent must match.")
        if self.run_status == GraphTurnStatus.INTERRUPTED:
            if self.status != ResponseStatus.NEEDS_CLARIFICATION or self.clarification is None:
                raise ValueError("Interrupted run requires an active clarification.")
        elif self.run_status == GraphTurnStatus.COMPLETED:
            if self.status == ResponseStatus.NEEDS_CLARIFICATION:
                raise ValueError("Completed run cannot require clarification.")
        elif self.status != ResponseStatus.FAILED or self.clarification is not None:
            raise ValueError("Failed run requires a terminal failed outcome.")
        active = self.context.active_clarification
        if self.clarification is not None:
            if active is None or active.id != self.clarification.id:
                raise ValueError("Context must reference the active clarification.")
        elif active is not None:
            raise ValueError("Terminal context cannot retain an active clarification.")
        if (
            self.presentation is not None
            and self.presentation.kind == "facility"
            and self.context.facility is not None
            and self.context.facility.target is not None
            and self.context.facility.target.station_name is not None
            and self.presentation.facility.station_name != self.context.facility.target.station_name
        ):
            raise ValueError("Facility presentation and context target must match.")
        return self

    @classmethod
    def from_run(
        cls,
        *,
        request_id: str,
        run: ConversationRun,
    ) -> ConversationRunResponseV4:
        outcome = run.outcome
        clarification = (
            project_structured_clarification(outcome.clarification)
            if outcome.clarification is not None
            else None
        )
        return cls(
            request_id=request_id,
            thread_id=run.thread_id,
            run_id=run.run_id,
            run_status=run.status,
            source=outcome.source,
            status=outcome.status,
            intent=outcome.intent,
            user_message=outcome.user_message,
            clarification_needed=outcome.clarification_needed,
            clarification=clarification,
            available_partial_info=outcome.available_partial_info,
            evidence=outcome.evidence,
            presentation=outcome.presentation,
            context=run.context,
        )


NegotiatedConversationRunResponse = ConversationRunResponse | ConversationRunResponseV4
