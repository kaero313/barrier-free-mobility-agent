from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Response, status

from barrier_free_mobility_agent.api.dependencies import (
    ContainerDependency,
    OwnerDependency,
    RequestIdDependency,
)
from barrier_free_mobility_agent.api.schemas.common import error_responses
from barrier_free_mobility_agent.api.schemas.threads import (
    ConversationRunResponse,
    ConversationRunResponseV4,
    CreateThreadResponse,
    NegotiatedConversationRunResponse,
    ResumeRunRequest,
    StartRunRequest,
)
from barrier_free_mobility_agent.application.conversations import ConversationRun

router = APIRouter(prefix="/v1", tags=["threads"])

ThreadId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
RunId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
RunResponseSchema = Annotated[
    Literal["3", "4"],
    Header(
        alias="X-Agent-Response-Schema",
        description="Opt in to the structured run response. Defaults to legacy schema 3.",
    ),
]
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$",
        description=(
            "Opaque key for one start or resume mutation. During the compatibility "
            "window, omitting it keeps the legacy synchronous execution path."
        ),
    ),
]


@router.post(
    "/threads",
    response_model=CreateThreadResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 429, 500),
)
async def create_thread(
    container: ContainerDependency,
    request_id: RequestIdDependency,
    owner: OwnerDependency,
) -> CreateThreadResponse:
    thread_id = await container.conversations.create_thread(owner=owner)
    return CreateThreadResponse(
        request_id=request_id,
        thread_id=thread_id,
        persistence=container.conversations.persistence,
    )


@router.delete(
    "/threads/{thread_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(401, 404, 422, 500),
)
async def delete_thread(
    thread_id: ThreadId,
    container: ContainerDependency,
    owner: OwnerDependency,
) -> Response:
    await container.conversations.delete_thread(thread_id, owner=owner)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/threads/{thread_id}/runs",
    response_model=NegotiatedConversationRunResponse,
    responses=error_responses(401, 404, 409, 422, 429, 500, 502, 503, 504),
)
async def start_run(
    thread_id: ThreadId,
    payload: StartRunRequest,
    container: ContainerDependency,
    request_id: RequestIdDependency,
    owner: OwnerDependency,
    response_schema: RunResponseSchema = "3",
    idempotency_key: IdempotencyKey = None,
) -> NegotiatedConversationRunResponse:
    if idempotency_key is not None and container.durable_conversations is not None:
        run = await container.durable_conversations.start_run(
            thread_id,
            payload.question,
            owner=owner,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
    else:
        run = await container.conversations.start_run(
            thread_id,
            payload.question,
            owner=owner,
            request_id=request_id,
        )
    return _project_run_response(run, request_id=request_id, response_schema=response_schema)


@router.get(
    "/runs/{run_id}",
    response_model=NegotiatedConversationRunResponse,
    responses=error_responses(401, 404, 422, 500),
)
async def get_run(
    run_id: RunId,
    container: ContainerDependency,
    request_id: RequestIdDependency,
    owner: OwnerDependency,
    response_schema: RunResponseSchema = "3",
) -> NegotiatedConversationRunResponse:
    run = await container.conversations.get_run(run_id, owner=owner)
    return _project_run_response(run, request_id=request_id, response_schema=response_schema)


@router.post(
    "/runs/{run_id}/resume",
    response_model=NegotiatedConversationRunResponse,
    responses=error_responses(401, 404, 409, 422, 429, 500, 502, 503, 504),
)
async def resume_run(
    run_id: RunId,
    payload: ResumeRunRequest,
    container: ContainerDependency,
    request_id: RequestIdDependency,
    owner: OwnerDependency,
    response_schema: RunResponseSchema = "3",
    idempotency_key: IdempotencyKey = None,
) -> NegotiatedConversationRunResponse:
    if idempotency_key is not None and container.durable_conversations is not None:
        run = await container.durable_conversations.resume_run(
            run_id,
            payload.answer,
            owner=owner,
            clarification_id=payload.clarification_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
    else:
        run = await container.conversations.resume_run(
            run_id,
            payload.answer,
            owner=owner,
            clarification_id=payload.clarification_id,
            request_id=request_id,
        )
    return _project_run_response(run, request_id=request_id, response_schema=response_schema)


def _project_run_response(
    run: ConversationRun,
    *,
    request_id: str,
    response_schema: Literal["3", "4"],
) -> NegotiatedConversationRunResponse:
    if response_schema == "4":
        return ConversationRunResponseV4.from_run(request_id=request_id, run=run)
    return ConversationRunResponse.from_run(request_id=request_id, run=run)
