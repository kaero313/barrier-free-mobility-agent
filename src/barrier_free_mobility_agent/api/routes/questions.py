from __future__ import annotations

from fastapi import APIRouter

from barrier_free_mobility_agent.api.dependencies import (
    ContainerDependency,
    OwnerDependency,
    RequestIdDependency,
)
from barrier_free_mobility_agent.api.schemas.common import error_responses
from barrier_free_mobility_agent.api.schemas.questions import (
    AnswerQuestionRequest,
    AnswerQuestionResponse,
)

router = APIRouter(prefix="/v1/accessibility", tags=["accessibility"])


@router.post(
    "/questions",
    response_model=AnswerQuestionResponse,
    responses=error_responses(401, 422, 429, 500, 502, 503, 504),
)
async def answer_question(
    payload: AnswerQuestionRequest,
    container: ContainerDependency,
    request_id: RequestIdDependency,
    _owner: OwnerDependency,
) -> AnswerQuestionResponse:
    outcome = await container.answer_question.execute(payload.question)
    return AnswerQuestionResponse.from_result(request_id=request_id, outcome=outcome)
