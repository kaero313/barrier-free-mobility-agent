from __future__ import annotations

import asyncio
import hmac
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from barrier_free_mobility_agent.agent.models import GraphTurnStatus, ResumeMode
from barrier_free_mobility_agent.agent.router import normalize_input
from barrier_free_mobility_agent.application.conversation_repository import (
    ConversationRepository,
    ConversationThread,
    PersistenceKind,
)
from barrier_free_mobility_agent.application.conversations import ConversationRun
from barrier_free_mobility_agent.domain.clarification import (
    FieldsClarificationAnswer,
    SingleTextClarificationAnswer,
    project_structured_clarification,
    validate_structured_answer,
)
from barrier_free_mobility_agent.domain.errors import (
    AgentStateIncompatibleError,
    ClarificationAnswerValidationError,
    ExecutionInProgressError,
    ExecutionRecoveryRequiredError,
    IdempotencyKeyReuseError,
    InvalidIdempotencyKeyError,
    InvalidQuestionError,
    QuestionTooLongError,
    RunNotFoundError,
    RunNotResumableError,
    StaleClarificationError,
)
from barrier_free_mobility_agent.domain.execution_queue import (
    ExecutionJobState,
    ExecutionOperation,
    ExecutionReservation,
    ResumeCommandExecutionPayload,
    ResumeExecutionPayload,
    ResumeNewTurnExecutionPayload,
    StartExecutionPayload,
)
from barrier_free_mobility_agent.domain.identity import LOCAL_OWNER_ID, OwnerId
from barrier_free_mobility_agent.domain.run_ledger import (
    RunEventType,
    RunExecutionCorrelation,
)

_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_TERMINAL_RUN_EVENTS = {
    RunEventType.RUN_INTERRUPTED,
    RunEventType.RUN_COMPLETED,
    RunEventType.RUN_FAILED,
}


@dataclass(frozen=True, slots=True)
class DurableConversationOptions:
    bounded_wait: timedelta = timedelta(seconds=60)
    poll_interval: timedelta = timedelta(milliseconds=50)

    def __post_init__(self) -> None:
        if self.bounded_wait < timedelta(0):
            raise ValueError("bounded_wait must not be negative")
        if self.poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be positive")


class DurableConversationCoordinator:
    """Reserve durable invocations and wait only for their public snapshot."""

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        idempotency_hmac_key: bytes,
        max_runs_per_thread: int,
        request_max_chars: int,
        flow_version: str,
        state_schema_version: str,
        options: DurableConversationOptions | None = None,
    ) -> None:
        if len(idempotency_hmac_key) < 32:
            raise ValueError("idempotency_hmac_key must contain at least 32 bytes")
        if max_runs_per_thread < 1:
            raise ValueError("max_runs_per_thread must be positive")
        if request_max_chars < 1:
            raise ValueError("request_max_chars must be positive")
        self._repository = repository
        self._hmac_key = bytes(idempotency_hmac_key)
        self._max_runs_per_thread = max_runs_per_thread
        self._request_max_chars = request_max_chars
        self._flow_version = flow_version
        self._state_schema_version = state_schema_version
        self._options = options or DurableConversationOptions()

    @property
    def persistence(self) -> PersistenceKind:
        return self._repository.persistence

    async def start_run(
        self,
        thread_id: str,
        question: str,
        *,
        idempotency_key: str,
        owner: OwnerId = LOCAL_OWNER_ID,
        request_id: str | None = None,
    ) -> ConversationRun:
        self._validate_idempotency_key(idempotency_key)
        normalized = self._normalize_request_text(question)
        payload = StartExecutionPayload(question=normalized)
        digest = self._idempotency_digest(
            operation=ExecutionOperation.START,
            target_id=thread_id,
            idempotency_key=idempotency_key,
        )
        fingerprint = self._request_fingerprint(
            operation=ExecutionOperation.START,
            payload=payload,
            clarification_id=None,
        )
        replay = await self._repository.find_execution_reservation(
            owner_id=str(owner),
            idempotency_digests=(digest,),
        )
        if replay is not None:
            if (
                replay.job.operation != ExecutionOperation.START
                or replay.correlation.thread_id != thread_id
                or not hmac.compare_digest(
                    replay.job.request_fingerprint,
                    fingerprint,
                )
            ):
                raise IdempotencyKeyReuseError()
            return await self._wait_for_public_snapshot(replay, owner_id=str(owner))
        thread = await self._repository.get_thread(thread_id, owner_id=str(owner))
        self._ensure_thread_compatible(thread)
        correlation = RunExecutionCorrelation(
            thread_id=thread_id,
            run_id=uuid4().hex,
            request_id=request_id or uuid4().hex,
            attempt=1,
        )
        reservation = await self._repository.reserve_initial_execution(
            correlation,
            owner_id=str(owner),
            capacity=self._max_runs_per_thread,
            idempotency_digest=digest,
            request_fingerprint=fingerprint,
            payload=payload,
            require_available_worker=True,
        )
        return await self._wait_for_public_snapshot(reservation, owner_id=str(owner))

    async def resume_run(
        self,
        run_id: str,
        answer: str | SingleTextClarificationAnswer | FieldsClarificationAnswer,
        *,
        idempotency_key: str,
        owner: OwnerId = LOCAL_OWNER_ID,
        clarification_id: str | None = None,
        request_id: str | None = None,
    ) -> ConversationRun:
        self._validate_idempotency_key(idempotency_key)
        normalized_answer = self._normalize_answer(answer)
        candidate_digests = tuple(
            self._idempotency_digest(
                operation=operation,
                target_id=run_id,
                idempotency_key=idempotency_key,
            )
            for operation in (
                ExecutionOperation.RESUME_COMMAND,
                ExecutionOperation.RESUME_NEW_TURN,
            )
        )
        replay = await self._repository.find_execution_reservation(
            owner_id=str(owner),
            idempotency_digests=candidate_digests,
        )
        if replay is not None:
            self._validate_resume_replay(
                replay,
                answer=normalized_answer,
                clarification_id=clarification_id,
            )
            return await self._wait_for_public_snapshot(replay, owner_id=str(owner))

        current = await self._repository.get_run(run_id, owner_id=str(owner))
        thread = await self._repository.get_thread(current.thread_id, owner_id=str(owner))
        self._ensure_thread_compatible(thread)
        if (
            current.status != GraphTurnStatus.INTERRUPTED
            or current.resume_mode == ResumeMode.NONE
            or thread.active_run_id != run_id
        ):
            raise RunNotResumableError(context={"run_id": run_id})
        if clarification_id is not None and (
            current.outcome.clarification is None
            or current.outcome.clarification.id != clarification_id
        ):
            raise StaleClarificationError()
        if isinstance(
            normalized_answer,
            (SingleTextClarificationAnswer, FieldsClarificationAnswer),
        ):
            active = current.outcome.clarification
            if active is None:
                raise RunNotResumableError(context={"run_id": run_id})
            validate_structured_answer(project_structured_clarification(active), normalized_answer)

        operation, payload = self._resume_payload(current, normalized_answer)
        digest = self._idempotency_digest(
            operation=operation,
            target_id=run_id,
            idempotency_key=idempotency_key,
        )
        fingerprint = self._request_fingerprint(
            operation=operation,
            payload=payload,
            clarification_id=clarification_id,
        )
        reservation = await self._repository.reserve_resume_execution(
            run_id,
            request_id=request_id or uuid4().hex,
            owner_id=str(owner),
            idempotency_digest=digest,
            request_fingerprint=fingerprint,
            payload=payload,
            require_available_worker=True,
        )
        return await self._wait_for_public_snapshot(reservation, owner_id=str(owner))

    async def _wait_for_public_snapshot(
        self,
        reservation: ExecutionReservation,
        *,
        owner_id: str,
    ) -> ConversationRun:
        deadline = monotonic() + self._options.bounded_wait.total_seconds()
        while True:
            job = await self._repository.get_execution_job(
                reservation.job.job_id,
                owner_id=owner_id,
            )
            if job.state in {ExecutionJobState.COMPLETED, ExecutionJobState.FAILED}:
                events = await self._repository.get_run_events(
                    job.correlation.run_id,
                    owner_id=owner_id,
                )
                has_snapshot_event = any(
                    event.correlation == job.correlation
                    and event.event_type in _TERMINAL_RUN_EVENTS
                    for event in events
                )
                if not has_snapshot_event:
                    raise ExecutionRecoveryRequiredError(
                        context={
                            "run_id": job.correlation.run_id,
                            "failure_code": job.failure_code or "SNAPSHOT_MISSING",
                        }
                    )
                try:
                    return await self._repository.get_run(
                        job.correlation.run_id,
                        owner_id=owner_id,
                    )
                except RunNotFoundError:
                    raise ExecutionRecoveryRequiredError(
                        context={"run_id": job.correlation.run_id}
                    ) from None
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ExecutionInProgressError(context={"run_id": job.correlation.run_id})
            await asyncio.sleep(min(self._options.poll_interval.total_seconds(), remaining))

    def _validate_resume_replay(
        self,
        reservation: ExecutionReservation,
        *,
        answer: str | SingleTextClarificationAnswer | FieldsClarificationAnswer,
        clarification_id: str | None,
    ) -> None:
        operation = reservation.job.operation
        payload = self._replay_payload(operation, answer)
        if payload is None:
            raise IdempotencyKeyReuseError()
        expected = self._request_fingerprint(
            operation=operation,
            payload=payload,
            clarification_id=clarification_id,
        )
        if not hmac.compare_digest(expected, reservation.job.request_fingerprint):
            raise IdempotencyKeyReuseError()

    def _resume_payload(
        self,
        current: ConversationRun,
        answer: str | SingleTextClarificationAnswer | FieldsClarificationAnswer,
    ) -> tuple[ExecutionOperation, ResumeExecutionPayload]:
        runner_answer: str | FieldsClarificationAnswer
        if isinstance(answer, SingleTextClarificationAnswer):
            runner_answer = answer.value
        else:
            runner_answer = answer
        if current.resume_mode == ResumeMode.COMMAND:
            return (
                ExecutionOperation.RESUME_COMMAND,
                ResumeCommandExecutionPayload(answer=runner_answer),
            )
        if current.resume_mode == ResumeMode.NEW_TURN and isinstance(runner_answer, str):
            return (
                ExecutionOperation.RESUME_NEW_TURN,
                ResumeNewTurnExecutionPayload(answer=runner_answer),
            )
        if current.resume_mode == ResumeMode.NEW_TURN:
            raise ClarificationAnswerValidationError(
                issues=(
                    {
                        "type": "clarification_answer_type_mismatch",
                        "loc": ("body", "answer", "mode"),
                        "msg": "현재 추가 질문에는 하나의 텍스트 답변이 필요합니다.",
                    },
                )
            )
        raise RunNotResumableError(context={"run_id": current.run_id})

    def _replay_payload(
        self,
        operation: ExecutionOperation,
        answer: str | SingleTextClarificationAnswer | FieldsClarificationAnswer,
    ) -> ResumeExecutionPayload | None:
        runner_answer: str | FieldsClarificationAnswer
        if isinstance(answer, SingleTextClarificationAnswer):
            runner_answer = answer.value
        else:
            runner_answer = answer
        if operation == ExecutionOperation.RESUME_COMMAND:
            return ResumeCommandExecutionPayload(answer=runner_answer)
        if operation == ExecutionOperation.RESUME_NEW_TURN and isinstance(runner_answer, str):
            return ResumeNewTurnExecutionPayload(answer=runner_answer)
        return None

    def _normalize_answer(
        self,
        answer: str | SingleTextClarificationAnswer | FieldsClarificationAnswer,
    ) -> str | SingleTextClarificationAnswer | FieldsClarificationAnswer:
        if isinstance(answer, str):
            return self._normalize_request_text(answer)
        if isinstance(answer, SingleTextClarificationAnswer):
            return answer.model_copy(update={"value": self._normalize_request_text(answer.value)})
        return answer

    def _normalize_request_text(self, value: str) -> str:
        normalized = normalize_input(value)
        if not normalized:
            raise InvalidQuestionError()
        if len(normalized) > self._request_max_chars:
            raise QuestionTooLongError(
                context={
                    "max_chars": self._request_max_chars,
                    "actual_chars": len(normalized),
                }
            )
        return normalized

    def _idempotency_digest(
        self,
        *,
        operation: ExecutionOperation,
        target_id: str,
        idempotency_key: str,
    ) -> str:
        return self._hmac(
            b"barrier-free-mobility:idempotency-scope:v1\0",
            operation.value.encode(),
            target_id.encode(),
            idempotency_key.encode(),
        )

    def _request_fingerprint(
        self,
        *,
        operation: ExecutionOperation,
        payload: StartExecutionPayload | ResumeExecutionPayload,
        clarification_id: str | None,
    ) -> str:
        canonical = json.dumps(
            {
                "operation": operation.value,
                "payload": payload.model_dump(mode="json"),
                "clarification_id": clarification_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return self._hmac(
            b"barrier-free-mobility:request-fingerprint:v1\0",
            canonical,
        )

    def _hmac(self, domain: bytes, *parts: bytes) -> str:
        framed = bytearray(domain)
        for part in parts:
            framed.extend(len(part).to_bytes(4, "big"))
            framed.extend(part)
        return hmac.new(self._hmac_key, framed, sha256).hexdigest()

    def _validate_idempotency_key(self, value: str) -> None:
        if _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
            raise InvalidIdempotencyKeyError()

    def _ensure_thread_compatible(self, thread: ConversationThread) -> None:
        if (
            thread.flow_version != self._flow_version
            or thread.state_schema_version != self._state_schema_version
        ):
            raise AgentStateIncompatibleError(
                context={
                    "expected_schema_version": self._state_schema_version,
                    "expected_flow_version": self._flow_version,
                }
            )
