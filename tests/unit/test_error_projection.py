from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from fastapi import status
from pydantic import ValidationError

from barrier_free_mobility_agent.api.error_projection import (
    project_agent_error,
    project_internal_error,
    project_request_validation_error,
    registered_agent_error_types,
)
from barrier_free_mobility_agent.api.schemas.common import (
    ERROR_CODE_MAX_CHARS,
    ERROR_MESSAGE_MAX_CHARS,
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

_REGISTERED_ERRORS: tuple[tuple[type[AgentError], int], ...] = (
    (OwnerAuthenticationError, status.HTTP_401_UNAUTHORIZED),
    (InvalidQuestionError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (InvalidIdempotencyKeyError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (QuestionTooLongError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (McpUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE),
    (McpContractError, status.HTTP_503_SERVICE_UNAVAILABLE),
    (McpDependencyDegradedError, status.HTTP_503_SERVICE_UNAVAILABLE),
    (McpToolExecutionError, status.HTTP_502_BAD_GATEWAY),
    (McpTimeoutError, status.HTTP_504_GATEWAY_TIMEOUT),
    (McpResponseValidationError, status.HTTP_502_BAD_GATEWAY),
    (PersistenceUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE),
    (AgentBudgetExceededError, status.HTTP_429_TOO_MANY_REQUESTS),
    (StateInputBudgetExceededError, status.HTTP_429_TOO_MANY_REQUESTS),
    (AgentPolicyViolationError, status.HTTP_500_INTERNAL_SERVER_ERROR),
    (ThreadNotFoundError, status.HTTP_404_NOT_FOUND),
    (RunNotFoundError, status.HTTP_404_NOT_FOUND),
    (ThreadBusyError, status.HTTP_409_CONFLICT),
    (RunNotResumableError, status.HTTP_409_CONFLICT),
    (StaleClarificationError, status.HTTP_409_CONFLICT),
    (AgentCapacityExceededError, status.HTTP_429_TOO_MANY_REQUESTS),
    (AgentStateIncompatibleError, status.HTTP_409_CONFLICT),
    (IdempotencyKeyReuseError, status.HTTP_409_CONFLICT),
    (ExecutionInProgressError, status.HTTP_409_CONFLICT),
    (ExecutionRecoveryRequiredError, status.HTTP_409_CONFLICT),
    (ExecutionWorkerUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE),
)


@pytest.mark.parametrize(("error_type", "status_code"), _REGISTERED_ERRORS)
def test_registered_agent_errors_discard_all_internal_detail(
    error_type: type[AgentError],
    status_code: int,
) -> None:
    error = error_type(
        detail="sensitive exception detail",
        context={
            "secret": "sensitive context",
            "thread_id": "private-thread",
            "tool": "private-tool",
        },
    )

    projected = project_agent_error(error, request_id="request-1")
    body = projected.response.model_dump(mode="json")

    assert projected.status_code == status_code
    assert body == {
        "schema_version": "2",
        "request_id": "request-1",
        "error": {
            "code": error_type.code,
            "message": error_type.public_message,
            "retryable": error_type.retryable,
            "detail": {},
        },
    }
    assert "sensitive" not in json.dumps(body)


def test_registry_contains_only_the_explicit_current_agent_error_types() -> None:
    assert registered_agent_error_types() == frozenset(
        error_type for error_type, _ in _REGISTERED_ERRORS
    )


@pytest.mark.parametrize(
    "error",
    [
        AgentError(detail="base detail", context={"secret": "base context"}),
        pytest.param(
            type(
                "UnregisteredError",
                (AgentError,),
                {
                    "code": "PRIVATE_CODE",
                    "public_message": "private message",
                    "retryable": True,
                },
            )(detail="private detail", context={"secret": "private context"}),
            id="unregistered-direct-subclass",
        ),
        pytest.param(
            type(
                "UnregisteredTimeoutError",
                (McpTimeoutError,),
                {
                    "code": "PRIVATE_TIMEOUT",
                    "public_message": "private timeout message",
                },
            )(context={"secret": "private inherited context"}),
            id="unregistered-derived-subclass",
        ),
    ],
)
def test_unregistered_agent_errors_fail_closed(error: AgentError) -> None:
    projected = project_agent_error(error, request_id="request-2")

    assert projected.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert projected.response.model_dump(mode="json") == {
        "schema_version": "2",
        "request_id": "request-2",
        "error": {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "요청을 처리하는 중 예기치 않은 오류가 발생했습니다.",
            "retryable": False,
            "detail": {},
        },
    }


def test_request_validation_projection_is_typed_bounded_and_value_free() -> None:
    raw_errors: list[Mapping[str, object]] = [
        {
            "type": "t" * (REQUEST_VALIDATION_TYPE_MAX_CHARS + 20),
            "loc": tuple(
                "l" * (REQUEST_VALIDATION_LOCATION_PART_MAX_CHARS + 20)
                for _ in range(REQUEST_VALIDATION_LOCATION_MAX_PARTS + 5)
            ),
            "msg": "m" * (REQUEST_VALIDATION_MESSAGE_MAX_CHARS + 20),
            "input": "secret-input",
            "ctx": {"secret": "secret-context"},
            "url": "https://internal.invalid/validation",
        }
        for _ in range(REQUEST_VALIDATION_MAX_ISSUES + 5)
    ]

    projected = project_request_validation_error(raw_errors, request_id="request-3")
    body = projected.response.model_dump(mode="json")
    issues = body["error"]["detail"]["issues"]

    assert projected.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert body["schema_version"] == "2"
    assert body["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert len(issues) == REQUEST_VALIDATION_MAX_ISSUES
    assert len(issues[0]["type"]) == REQUEST_VALIDATION_TYPE_MAX_CHARS
    assert len(issues[0]["location"]) == REQUEST_VALIDATION_LOCATION_MAX_PARTS
    assert all(
        len(part) == REQUEST_VALIDATION_LOCATION_PART_MAX_CHARS for part in issues[0]["location"]
    )
    assert len(issues[0]["message"]) == REQUEST_VALIDATION_MESSAGE_MAX_CHARS
    serialized = json.dumps(body)
    assert "secret-input" not in serialized
    assert "secret-context" not in serialized
    assert "internal.invalid" not in serialized


def test_empty_request_validation_errors_receive_one_safe_fallback_issue() -> None:
    projected = project_request_validation_error([], request_id="request-4")

    assert projected.response.model_dump(mode="json")["error"]["detail"] == {
        "issues": [
            {
                "type": "validation_error",
                "location": ["request"],
                "message": "요청 형식이 올바르지 않습니다.",
            }
        ]
    }


def test_malformed_validation_issue_values_cannot_run_user_stringification() -> None:
    class ExplodingString:
        def __str__(self) -> str:
            raise AssertionError("untrusted __str__ must not run")

    projected = project_request_validation_error(
        [
            {
                "type": ExplodingString(),
                "loc": (ExplodingString(), 7, True),
                "msg": ExplodingString(),
            }
        ],
        request_id="request-malformed",
    )

    assert projected.response.model_dump(mode="json")["error"]["detail"] == {
        "issues": [
            {
                "type": "validation_error",
                "location": ["request", "7", "request"],
                "message": "요청 형식이 올바르지 않습니다.",
            }
        ]
    }


def test_validation_issue_model_rejects_values_outside_the_public_bounds() -> None:
    with pytest.raises(ValidationError):
        RequestValidationIssue(type="", location=(), message="")


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("lowercase_code", "valid"),
        ("A" * (ERROR_CODE_MAX_CHARS + 1), "valid"),
        ("VALID_CODE", ""),
        ("VALID_CODE", "m" * (ERROR_MESSAGE_MAX_CHARS + 1)),
    ],
)
def test_error_body_rejects_unbounded_or_noncanonical_public_text(
    code: str,
    message: str,
) -> None:
    with pytest.raises(ValidationError):
        ErrorBody(code=code, message=message)


def test_error_body_enforces_the_code_detail_invariant_in_both_directions() -> None:
    validation_detail = RequestValidationErrorDetail(
        issues=(
            RequestValidationIssue(
                type="missing",
                location=("body", "question"),
                message="Field required",
            ),
        )
    )

    with pytest.raises(ValidationError, match="must be paired"):
        ErrorBody(
            code="REQUEST_VALIDATION_ERROR",
            message="invalid request",
            detail=EmptyErrorDetail(),
        )
    with pytest.raises(ValidationError, match="must be paired"):
        ErrorBody(
            code="MCP_TIMEOUT",
            message="timeout",
            detail=validation_detail,
        )


def test_internal_projection_uses_the_v2_safe_envelope() -> None:
    projected = project_internal_error(request_id="request-5")

    assert projected.response.schema_version == "2"
    assert projected.response.error.code == "INTERNAL_SERVER_ERROR"
    assert projected.response.error.detail.model_dump(mode="json") == {}
    schema = ErrorResponse.model_json_schema()
    definitions = schema["$defs"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"] == {
        "const": "2",
        "default": "2",
        "title": "Schema Version",
        "type": "string",
    }
    for model_name in (
        "EmptyErrorDetail",
        "ErrorBody",
        "RequestValidationErrorDetail",
        "RequestValidationIssue",
    ):
        assert definitions[model_name]["additionalProperties"] is False
    assert definitions["ErrorBody"]["properties"]["detail"]["anyOf"] == [
        {"$ref": "#/$defs/EmptyErrorDetail"},
        {"$ref": "#/$defs/RequestValidationErrorDetail"},
    ]
    issue_schema = definitions["RequestValidationIssue"]["properties"]
    error_body_schema = definitions["ErrorBody"]["properties"]
    assert error_body_schema["code"]["maxLength"] == ERROR_CODE_MAX_CHARS
    assert error_body_schema["code"]["pattern"] == r"^[A-Z][A-Z0-9_]*$"
    assert error_body_schema["message"]["maxLength"] == ERROR_MESSAGE_MAX_CHARS
    assert issue_schema["type"]["maxLength"] == REQUEST_VALIDATION_TYPE_MAX_CHARS
    assert issue_schema["location"]["maxItems"] == REQUEST_VALIDATION_LOCATION_MAX_PARTS
    assert (
        issue_schema["location"]["items"]["maxLength"] == REQUEST_VALIDATION_LOCATION_PART_MAX_CHARS
    )
    assert issue_schema["message"]["maxLength"] == REQUEST_VALIDATION_MESSAGE_MAX_CHARS
    assert (
        definitions["RequestValidationErrorDetail"]["properties"]["issues"]["maxItems"]
        == REQUEST_VALIDATION_MAX_ISSUES
    )
