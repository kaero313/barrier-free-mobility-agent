from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ERROR_CODE_MAX_CHARS = 100
ERROR_CODE_PATTERN = r"^[A-Z][A-Z0-9_]*$"
ERROR_MESSAGE_MAX_CHARS = 500
REQUEST_VALIDATION_MAX_ISSUES = 32
REQUEST_VALIDATION_TYPE_MAX_CHARS = 100
REQUEST_VALIDATION_LOCATION_MAX_PARTS = 16
REQUEST_VALIDATION_LOCATION_PART_MAX_CHARS = 100
REQUEST_VALIDATION_MESSAGE_MAX_CHARS = 300

ValidationIssueType = Annotated[
    str,
    Field(min_length=1, max_length=REQUEST_VALIDATION_TYPE_MAX_CHARS),
]
ValidationLocationPart = Annotated[
    str,
    Field(min_length=1, max_length=REQUEST_VALIDATION_LOCATION_PART_MAX_CHARS),
]
ValidationIssueMessage = Annotated[
    str,
    Field(min_length=1, max_length=REQUEST_VALIDATION_MESSAGE_MAX_CHARS),
]


class EmptyErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: ValidationIssueType
    location: tuple[ValidationLocationPart, ...] = Field(
        min_length=1,
        max_length=REQUEST_VALIDATION_LOCATION_MAX_PARTS,
    )
    message: ValidationIssueMessage


class RequestValidationErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: tuple[RequestValidationIssue, ...] = Field(
        min_length=1,
        max_length=REQUEST_VALIDATION_MAX_ISSUES,
    )


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(
        min_length=1,
        max_length=ERROR_CODE_MAX_CHARS,
        pattern=ERROR_CODE_PATTERN,
    )
    message: str = Field(min_length=1, max_length=ERROR_MESSAGE_MAX_CHARS)
    retryable: bool = False
    detail: EmptyErrorDetail | RequestValidationErrorDetail = Field(
        default_factory=EmptyErrorDetail
    )

    @model_validator(mode="after")
    def validate_code_detail_invariant(self) -> ErrorBody:
        is_request_validation = self.code == "REQUEST_VALIDATION_ERROR"
        has_validation_issues = isinstance(self.detail, RequestValidationErrorDetail)
        if is_request_validation != has_validation_issues:
            raise ValueError(
                "REQUEST_VALIDATION_ERROR must be paired with request validation issues."
            )
        return self


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2"] = "2"
    request_id: str
    error: ErrorBody


_ERROR_DESCRIPTIONS = {
    401: "The request owner could not be authenticated.",
    404: "The requested thread or run was not found.",
    409: "The thread or run state does not allow this operation.",
    422: "The request could not be validated or normalized.",
    429: "An Agent capacity or execution budget was exceeded.",
    500: "The Agent could not safely complete the operation.",
    502: "The MCP Tool returned an execution or validation error.",
    503: "An MCP or durable execution dependency is unavailable.",
    504: "The MCP Tool call timed out.",
}


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Declare the same versioned error envelope used by runtime handlers."""

    return {
        code: {
            "model": ErrorResponse,
            "description": _ERROR_DESCRIPTIONS[code],
        }
        for code in status_codes
    }
