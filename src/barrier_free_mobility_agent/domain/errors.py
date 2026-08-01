from __future__ import annotations

from typing import Any, ClassVar


class AgentError(Exception):
    code: ClassVar[str] = "AGENT_ERROR"
    public_message: ClassVar[str] = "요청을 처리하지 못했습니다."
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        *,
        detail: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.detail = detail
        self.context = context or {}
        super().__init__(detail or self.public_message)


class OwnerAuthenticationError(AgentError):
    code = "OWNER_AUTHENTICATION_REQUIRED"
    public_message = "요청 소유자를 확인할 수 없습니다."


class InvalidQuestionError(AgentError):
    code = "INVALID_QUESTION"
    public_message = "질문을 입력해 주세요."


class InvalidIdempotencyKeyError(AgentError):
    code = "INVALID_IDEMPOTENCY_KEY"
    public_message = "중복 방지 키 형식이 올바르지 않습니다."


class QuestionTooLongError(AgentError):
    code = "QUESTION_TOO_LONG"
    public_message = "현재 질문이 MCP 입력 제한보다 깁니다."


class McpUnavailableError(AgentError):
    code = "MCP_UNAVAILABLE"
    public_message = "이동 정보 서비스를 일시적으로 사용할 수 없습니다."
    retryable = True


class McpContractError(AgentError):
    code = "MCP_CONTRACT_ERROR"
    public_message = "이동 정보 서비스의 Tool 계약이 호환되지 않습니다."


class McpDependencyDegradedError(AgentError):
    code = "MCP_DEGRADED"
    public_message = "이동 정보 서비스의 일부 의존성이 정상 상태가 아닙니다."
    retryable = True


class McpToolExecutionError(AgentError):
    code = "MCP_TOOL_EXECUTION_ERROR"
    public_message = "이동 정보를 조회하는 중 오류가 발생했습니다."


class McpTimeoutError(AgentError):
    code = "MCP_TIMEOUT"
    public_message = "이동 정보 조회 시간이 초과되었습니다."
    retryable = True


class McpResponseValidationError(AgentError):
    code = "MCP_RESPONSE_VALIDATION_ERROR"
    public_message = "이동 정보 응답을 안전하게 검증하지 못했습니다."


class PersistenceUnavailableError(AgentError):
    code = "PERSISTENCE_UNAVAILABLE"
    public_message = "데이터 저장소를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
    retryable = True


class AgentBudgetExceededError(AgentError):
    code = "AGENT_BUDGET_EXCEEDED"
    public_message = "안전한 처리 한도를 초과했습니다. 질문 범위를 좁혀 다시 시도해 주세요."


class StateInputBudgetExceededError(AgentBudgetExceededError):
    """A user-correctable state merge rejected before LangGraph accepts the input."""


class AgentPolicyViolationError(AgentError):
    code = "AGENT_POLICY_VIOLATION"
    public_message = "허용되지 않은 실행 계획을 차단했습니다."


class ThreadNotFoundError(AgentError):
    code = "THREAD_NOT_FOUND"
    public_message = "대화 thread를 찾을 수 없습니다."


class RunNotFoundError(AgentError):
    code = "RUN_NOT_FOUND"
    public_message = "Agent run을 찾을 수 없습니다."


class ThreadBusyError(AgentError):
    code = "THREAD_BUSY"
    public_message = "이 대화에는 먼저 완료해야 할 명확화 요청이 있습니다."


class RunNotResumableError(AgentError):
    code = "RUN_NOT_RESUMABLE"
    public_message = "이 Agent run은 재개할 수 있는 상태가 아닙니다."


class StaleClarificationError(AgentError):
    code = "STALE_CLARIFICATION"
    public_message = "추가 질문이 갱신되었습니다. 최신 상태를 확인한 뒤 다시 답해 주세요."


class ClarificationAnswerValidationError(AgentError):
    """A public-safe semantic validation failure for the active answer contract."""

    code = "CLARIFICATION_ANSWER_INVALID"
    public_message = "추가 질문의 답변 형식이 올바르지 않습니다."

    def __init__(self, *, issues: tuple[dict[str, object], ...]) -> None:
        self.issues = issues
        super().__init__(context={"issue_count": len(issues)})


class AgentCapacityExceededError(AgentError):
    code = "AGENT_CAPACITY_EXCEEDED"
    public_message = "현재 생성 가능한 대화 또는 실행 한도를 초과했습니다."


class AgentStateIncompatibleError(AgentError):
    code = "AGENT_STATE_INCOMPATIBLE"
    public_message = (
        "이 대화 상태는 현재 Agent 버전과 호환되지 않습니다. 새 thread를 시작해 주세요."
    )


class IdempotencyKeyReuseError(AgentError):
    code = "IDEMPOTENCY_KEY_REUSED"
    public_message = "같은 중복 방지 키를 다른 요청에 사용할 수 없습니다."


class ExecutionJobNotFoundError(AgentError):
    code = "EXECUTION_JOB_NOT_FOUND"
    public_message = "Agent 실행 작업을 찾을 수 없습니다."


class ExecutionLeaseLostError(AgentError):
    code = "EXECUTION_LEASE_LOST"
    public_message = "Agent 실행 작업의 처리 권한이 만료되었거나 변경되었습니다."


class ExecutionWorkerUnavailableError(AgentError):
    code = "EXECUTION_WORKER_UNAVAILABLE"
    public_message = (
        "Agent 실행 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
    )
    retryable = True


class ExecutionWorkerRegistrationLostError(AgentError):
    """Internal fencing failure for a stale or replaced worker process."""

    code = "EXECUTION_WORKER_REGISTRATION_LOST"
    public_message = "Agent 실행 Worker 등록이 만료되었거나 변경되었습니다."


class ExecutionInProgressError(AgentError):
    code = "EXECUTION_IN_PROGRESS"
    public_message = (
        "요청을 안전하게 접수했으며 Agent가 계속 처리하고 있습니다. 잠시 후 다시 확인해 주세요."
    )
    retryable = True


class ExecutionRecoveryRequiredError(AgentError):
    code = "EXECUTION_RECOVERY_REQUIRED"
    public_message = (
        "실행 상태를 자동으로 확정할 수 없어 안전하게 중단했습니다. 새 요청으로 다시 시도해 주세요."
    )
