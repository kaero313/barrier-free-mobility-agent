from __future__ import annotations

from contextlib import suppress

from fastapi import APIRouter, Response, status

from barrier_free_mobility_agent.api.dependencies import ContainerDependency
from barrier_free_mobility_agent.api.schemas.health import (
    LivenessResponse,
    McpReadiness,
    PersistenceReadiness,
    ReadinessDependencies,
    ReadinessResponse,
)
from barrier_free_mobility_agent.domain.errors import AgentError
from barrier_free_mobility_agent.domain.models import ContractStatus

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=LivenessResponse)
async def liveness(
    container: ContainerDependency,
) -> LivenessResponse:
    return LivenessResponse(
        service=container.settings.app_name,
        version=container.settings.app_version,
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(
    response: Response,
    container: ContainerDependency,
) -> ReadinessResponse:
    with suppress(AgentError):
        await container.mcp_gateway.refresh_if_stale()
    snapshot = container.mcp_gateway.contract_snapshot
    persistence_ready = await container.conversations.is_ready()
    ready = snapshot.status == ContractStatus.READY and persistence_ready
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        service=container.settings.app_name,
        version=container.settings.app_version,
        dependencies=ReadinessDependencies(
            mobility_mcp=McpReadiness(
                status=snapshot.status,
                checked_at=snapshot.checked_at,
                tool_count=len(snapshot.tool_names),
                capabilities=snapshot.capabilities,
                schema_fingerprint=snapshot.schema_fingerprint,
                error_code=snapshot.error_code,
            ),
            persistence=PersistenceReadiness(
                status="ready" if persistence_ready else "unavailable",
                backend=container.settings.persistence_backend.value,
            ),
        ),
    )
