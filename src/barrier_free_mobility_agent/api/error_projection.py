from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from fastapi import status

from barrier_free_mobility_agent.api.schemas.common import (
    REQUEST_VALIDATION_LOCATION_MAX_PARTS,
    REQUEST_VALIDATION_LOCATION_PART_MAX_CHARS,
    REQUEST_VALIDATION_MAX_ISSUES,
    REQUEST_VALIDATION_MESSAGE_MAX_CHARS,
    REQUEST_VALIDATION_TYPE_MAX_CHARS,
    EmptyErrorDetail,
    ErrorBody,
    ErrorResponse,
    RequestValidationErrorDetail,
    RequestValidationIssue,
)
from barrier_free_mobility_agent.domain.errors import (
    AgentBudgetExceededError,
    AgentCapacityExceededError,
    AgentError,
    AgentPolicyViolationError,
    AgentStateIncompatibleError,
    ClarificationAnswerValidationError,
    ExecutionInProgressError,
    ExecutionRecoveryRequiredError,
    ExecutionWorkerUnavailableError,
    IdempotencyKeyReuseError,
    InvalidIdempotencyKeyError,
    InvalidQuestionError,
    McpContractError,
    McpDependencyDegradedError,
    McpResponseValidationError,
    McpTimeoutError,
    McpToolExecutionError,
    McpUnavailableError,
    OwnerAuthenticationError,
    PersistenceUnavailableError,
    QuestionTooLongError,
    RunNotFoundError,
    RunNotResumableError,
    StaleClarificationError,
    StateInputBudgetExceededError,
    ThreadBusyError,
    ThreadNotFoundError,
)

_INTERNAL_ERROR_CODE = "INTERNAL_SERVER_ERROR"
_INTERNAL_ERROR_MESSAGE = "요청을 처리하는 중 예기치 않은 오류가 발생했습니다."
_REQUEST_VALIDATION_ERROR_CODE = "REQUEST_VALIDATION_ERROR"
_REQUEST_VALIDATION_ERROR_MESSAGE = "요청 형식이 올바르지 않습니다."


@dataclass(frozen=True, slots=True)
class PublicErrorSpec:
    status_code: int
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class ProjectedErrorResponse:
    status_code: int
    response: ErrorResponse


def _agent_error_spec(
    error_type: type[AgentError],
    status_code: int,
) -> PublicErrorSpec:
    return PublicErrorSpec(
        status_code=status_code,
        code=error_type.code,
        message=error_type.public_message,
        retryable=error_type.retryable,
    )


_AGENT_ERROR_REGISTRY: Final[Mapping[type[AgentError], PublicErrorSpec]] = MappingProxyType(
    {
        OwnerAuthenticationError: _agent_error_spec(
            OwnerAuthenticationError, status.HTTP_401_UNAUTHORIZED
        ),
        AgentBudgetExceededError: _agent_error_spec(
            AgentBudgetExceededError, status.HTTP_429_TOO_MANY_REQUESTS
        ),
        StateInputBudgetExceededError: _agent_error_spec(
            StateInputBudgetExceededError, status.HTTP_429_TOO_MANY_REQUESTS
        ),
        AgentCapacityExceededError: _agent_error_spec(
            AgentCapacityExceededError, status.HTTP_429_TOO_MANY_REQUESTS
        ),
        AgentPolicyViolationError: _agent_error_spec(
            AgentPolicyViolationError, status.HTTP_500_INTERNAL_SERVER_ERROR
        ),
        AgentStateIncompatibleError: _agent_error_spec(
            AgentStateIncompatibleError, status.HTTP_409_CONFLICT
        ),
        InvalidQuestionError: _agent_error_spec(
            InvalidQuestionError, status.HTTP_422_UNPROCESSABLE_CONTENT
        ),
        InvalidIdempotencyKeyError: _agent_error_spec(
            InvalidIdempotencyKeyError, status.HTTP_422_UNPROCESSABLE_CONTENT
        ),
        QuestionTooLongError: _agent_error_spec(
            QuestionTooLongError, status.HTTP_422_UNPROCESSABLE_CONTENT
        ),
        McpUnavailableError: _agent_error_spec(
            McpUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        McpContractError: _agent_error_spec(McpContractError, status.HTTP_503_SERVICE_UNAVAILABLE),
        McpDependencyDegradedError: _agent_error_spec(
            McpDependencyDegradedError, status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        McpToolExecutionError: _agent_error_spec(
            McpToolExecutionError, status.HTTP_502_BAD_GATEWAY
        ),
        McpTimeoutError: _agent_error_spec(McpTimeoutError, status.HTTP_504_GATEWAY_TIMEOUT),
        McpResponseValidationError: _agent_error_spec(
            McpResponseValidationError, status.HTTP_502_BAD_GATEWAY
        ),
        PersistenceUnavailableError: _agent_error_spec(
            PersistenceUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        ThreadNotFoundError: _agent_error_spec(ThreadNotFoundError, status.HTTP_404_NOT_FOUND),
        RunNotFoundError: _agent_error_spec(RunNotFoundError, status.HTTP_404_NOT_FOUND),
        ThreadBusyError: _agent_error_spec(ThreadBusyError, status.HTTP_409_CONFLICT),
        RunNotResumableError: _agent_error_spec(RunNotResumableError, status.HTTP_409_CONFLICT),
        StaleClarificationError: _agent_error_spec(
            StaleClarificationError, status.HTTP_409_CONFLICT
        ),
        IdempotencyKeyReuseError: _agent_error_spec(
            IdempotencyKeyReuseError, status.HTTP_409_CONFLICT
        ),
        ExecutionInProgressError: _agent_error_spec(
            ExecutionInProgressError, status.HTTP_409_CONFLICT
        ),
        ExecutionRecoveryRequiredError: _agent_error_spec(
            ExecutionRecoveryRequiredError, status.HTTP_409_CONFLICT
        ),
        ExecutionWorkerUnavailableError: _agent_error_spec(
            ExecutionWorkerUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE
        ),
    }
)

_INTERNAL_ERROR_SPEC = PublicErrorSpec(
    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    code=_INTERNAL_ERROR_CODE,
    message=_INTERNAL_ERROR_MESSAGE,
    retryable=False,
)
_REQUEST_VALIDATION_ERROR_SPEC = PublicErrorSpec(
    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    code=_REQUEST_VALIDATION_ERROR_CODE,
    message=_REQUEST_VALIDATION_ERROR_MESSAGE,
    retryable=False,
)


def registered_agent_error_types() -> frozenset[type[AgentError]]:
    return frozenset(_AGENT_ERROR_REGISTRY)


def project_agent_error(exc: AgentError, *, request_id: str) -> ProjectedErrorResponse:
    if isinstance(exc, ClarificationAnswerValidationError):
        return project_request_validation_error(exc.issues, request_id=request_id)
    spec = _AGENT_ERROR_REGISTRY.get(type(exc), _INTERNAL_ERROR_SPEC)
    return _project(spec, request_id=request_id, detail=EmptyErrorDetail())


def project_request_validation_error(
    errors: Iterable[Mapping[str, object]],
    *,
    request_id: str,
) -> ProjectedErrorResponse:
    issues: list[RequestValidationIssue] = []
    for error in errors:
        if len(issues) == REQUEST_VALIDATION_MAX_ISSUES:
            break
        issues.append(_project_validation_issue(error))
    if not issues:
        issues.append(
            RequestValidationIssue(
                type="validation_error",
                location=("request",),
                message=_REQUEST_VALIDATION_ERROR_MESSAGE,
            )
        )
    return _project(
        _REQUEST_VALIDATION_ERROR_SPEC,
        request_id=request_id,
        detail=RequestValidationErrorDetail(issues=tuple(issues)),
    )


def project_internal_error(*, request_id: str) -> ProjectedErrorResponse:
    return _project(_INTERNAL_ERROR_SPEC, request_id=request_id, detail=EmptyErrorDetail())


def _project_validation_issue(error: Mapping[str, object]) -> RequestValidationIssue:
    raw_location = error.get("loc")
    if isinstance(raw_location, (list, tuple)):
        location = tuple(
            _bounded_text(
                part,
                fallback="request",
                max_chars=REQUEST_VALIDATION_LOCATION_PART_MAX_CHARS,
                allow_integer=True,
            )
            for part in raw_location[:REQUEST_VALIDATION_LOCATION_MAX_PARTS]
        )
    else:
        location = ()
    if not location:
        location = ("request",)
    return RequestValidationIssue(
        type=_bounded_text(
            error.get("type"),
            fallback="validation_error",
            max_chars=REQUEST_VALIDATION_TYPE_MAX_CHARS,
        ),
        location=location,
        message=_bounded_text(
            error.get("msg"),
            fallback=_REQUEST_VALIDATION_ERROR_MESSAGE,
            max_chars=REQUEST_VALIDATION_MESSAGE_MAX_CHARS,
        ),
    )


def _bounded_text(
    value: object,
    *,
    fallback: str,
    max_chars: int,
    allow_integer: bool = False,
) -> str:
    if type(value) is str:
        text = value
    elif allow_integer and type(value) is int:
        text = str(value)
    else:
        return fallback[:max_chars]
    normalized = str.strip(text)
    return (normalized or fallback)[:max_chars]


def _project(
    spec: PublicErrorSpec,
    *,
    request_id: str,
    detail: EmptyErrorDetail | RequestValidationErrorDetail,
) -> ProjectedErrorResponse:
    return ProjectedErrorResponse(
        status_code=spec.status_code,
        response=ErrorResponse(
            request_id=request_id,
            error=ErrorBody(
                code=spec.code,
                message=spec.message,
                retryable=spec.retryable,
                detail=detail,
            ),
        ),
    )
