from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import Error as PsycopgError
from psycopg_pool import PoolClosed, PoolTimeout

from barrier_free_mobility_agent.api.container import build_container
from barrier_free_mobility_agent.api.dependencies import get_request_id
from barrier_free_mobility_agent.api.error_projection import (
    project_agent_error,
    project_internal_error,
    project_request_validation_error,
)
from barrier_free_mobility_agent.api.middleware import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
)
from barrier_free_mobility_agent.api.routes import health, questions, threads
from barrier_free_mobility_agent.application.ports import MobilityMcpGateway
from barrier_free_mobility_agent.config import Settings, get_settings
from barrier_free_mobility_agent.domain.errors import AgentError, PersistenceUnavailableError
from barrier_free_mobility_agent.integrations.mcp.gateway import LangChainMcpGateway
from barrier_free_mobility_agent.observability.logging import configure_logging
from barrier_free_mobility_agent.persistence.runtime import open_persistence

logger = logging.getLogger(__name__)

_PERSISTENCE_UNAVAILABLE_EXCEPTIONS = (PsycopgError, PoolTimeout, PoolClosed)


def create_app(
    *,
    settings: Settings | None = None,
    mcp_gateway: MobilityMcpGateway | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging(active_settings.log_level, active_settings.log_format)
    gateway = mcp_gateway or LangChainMcpGateway(active_settings)

    @asynccontextmanager
    async def lifespan(running_app: FastAPI) -> AsyncIterator[None]:
        try:
            async with open_persistence(active_settings) as persistence:
                running_app.state.container = build_container(
                    active_settings,
                    gateway,
                    checkpointer=persistence.checkpointer,
                    conversation_repository=persistence.conversations,
                )
                try:
                    await gateway.initialize()
                except AgentError:
                    logger.exception("MCP contract initialization failed")
                    if active_settings.mcp_startup_required:
                        raise
                yield
        finally:
            await gateway.close()

    app = FastAPI(
        title=active_settings.app_name,
        version=active_settings.app_version,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(questions.router)
    app.include_router(threads.router)

    @app.exception_handler(AgentError)
    async def handle_agent_error(request: Request, exc: AgentError) -> JSONResponse:
        request_id = get_request_id(request)
        projected = project_agent_error(exc, request_id=request_id)
        logger.warning(
            "Agent request failed: type=%s public_code=%s retryable=%s",
            type(exc).__name__,
            projected.response.error.code,
            projected.response.error.retryable,
        )
        return JSONResponse(
            status_code=projected.status_code,
            content=projected.response.model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        request_id = get_request_id(request)
        projected = project_request_validation_error(exc.errors(), request_id=request_id)
        return JSONResponse(
            status_code=projected.status_code,
            content=projected.response.model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = get_request_id(request)
        if isinstance(exc, _PERSISTENCE_UNAVAILABLE_EXCEPTIONS):
            projected = project_agent_error(
                PersistenceUnavailableError(),
                request_id=request_id,
            )
            logger.warning(
                "Persistence request failed: type=%s public_code=%s retryable=%s",
                type(exc).__name__,
                projected.response.error.code,
                projected.response.error.retryable,
            )
        else:
            projected = project_internal_error(request_id=request_id)
            logger.error(
                "Unexpected request failure: type=%s public_code=%s retryable=%s",
                type(exc).__name__,
                projected.response.error.code,
                projected.response.error.retryable,
            )
        return JSONResponse(
            status_code=projected.status_code,
            content=projected.response.model_dump(mode="json"),
            headers={REQUEST_ID_HEADER: request_id},
        )

    return app
